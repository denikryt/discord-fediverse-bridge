/** Rejected bootstrap never needs a Fluxer SDK import or bot credential. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {runFluxerNodeService} from '../src/main.ts';

/** Select an isolated environment and restore every value after the scenario. */
async function withEnv(vars, action) {
  const before = new Map(Object.keys(vars).map(key=>[key, process.env[key]]));
  try {
    for (const [key,value] of Object.entries(vars)) process.env[key]=value;
    return await action();
  } finally {
    for (const [key,value] of before) {
      if (value === undefined) delete process.env[key];
      else process.env[key]=value;
    }
  }
}

test('missing mandatory operator settings fail before a discovery HTTP request', async()=>{
  await withEnv({FLUXER_CONNECTOR_KEY:'', FLUXER_ORIGIN:'https://example.test'},async()=>{
    await assert.rejects(runFluxerNodeService(),/invalid Fluxer Node deployment configuration/);
  });
});

test('reserved Discord connector key refuses Node startup before discovery', async()=>{
  await withEnv({FLUXER_CONNECTOR_KEY:'discord-primary',
    FLUXER_ORIGIN:'https://fx.test', FLUXER_BOT_TOKEN_FILE:'/not-mounted',
    FLUXER_INTERNAL_BEARER:'valid-secret',
    FLUXER_APPROVED_API_HOST:'api.fx.test',
    FLUXER_APPROVED_GATEWAY_HOST:'gateway.fx.test',
    FLUXER_RPC_PORT:'8769',
    FLUXER_PYTHON_INGRESS_URL:'http://127.0.0.1:8080/internal/fluxer/events',
  },async()=>{
    await assert.rejects(runFluxerNodeService(),/invalid Fluxer Node deployment configuration/);
  });
});

test('private bearer rejects unsafe HTTP header values before discovery or SDK', async()=>{
  const basic = {
    FLUXER_CONNECTOR_KEY:'fx', FLUXER_ORIGIN:'https://fx.test',
    FLUXER_BOT_TOKEN_FILE:'/no-token', FLUXER_APPROVED_API_HOST:'api.fx.test',
    FLUXER_APPROVED_GATEWAY_HOST:'gateway.fx.test', FLUXER_RPC_PORT:'8769',
    FLUXER_PYTHON_INGRESS_URL:'http://127.0.0.1:8080/internal/fluxer/events',
  };
  for (const bearer of ['1234567', '1234567é', 'секретсекрет', 'abc\rdefgh',
    'abc\ndefgh', 'abc\tdefgh', 'abc defgh', 'abcdefgh\x7f']) {
    await withEnv({...basic, FLUXER_INTERNAL_BEARER:bearer}, async()=>{
      await assert.rejects(runFluxerNodeService(),
        /invalid Fluxer Node deployment configuration/);
    });
  }
});

test('tokenless discovery precedes secret read; missing Node mount refuses SDK startup',async()=>{
  const seen=[];
  const server=createServer((req,res)=>{
    seen.push([req.url, req.headers.authorization]);
    const port=server.address().port;
    res.end(JSON.stringify({endpoints:{
      api_public:`http://127.0.0.1:${port}`,
      gateway:`ws://127.0.0.1:${port}`,
    }}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const port=server.address().port;
  try {
    await withEnv({
      FLUXER_CONNECTOR_KEY:'fx', FLUXER_ORIGIN:`http://127.0.0.1:${port}`,
      FLUXER_BOT_TOKEN_FILE:'/not-present-on-test-system/fluxer-token',
      FLUXER_INTERNAL_BEARER:'test-ipc-secret',
      FLUXER_APPROVED_API_HOST:'127.0.0.1',
      FLUXER_APPROVED_GATEWAY_HOST:'127.0.0.1',
      FLUXER_RPC_PORT:'8769',
      FLUXER_PYTHON_INGRESS_URL:'http://127.0.0.1:8080/internal/fluxer/events',
      FLUXER_ALLOW_INSECURE_LOCAL_DEVELOPMENT:'true',
    }, async()=>{
      await assert.rejects(runFluxerNodeService(),/secret file is unavailable/);
    });
    assert.deepEqual(seen,[['/.well-known/fluxer',undefined]]);
  }finally{await new Promise(resolve=>server.close(resolve));}
});

test('unexpected API host fails before Node-only bot token file is accessed',async()=>{
  const server=createServer((req,res)=>{
    res.end(JSON.stringify({endpoints:{
      api_public:'http://127.0.0.1:8091',gateway:'ws://127.0.0.1:8091',
    }}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    await withEnv({
      FLUXER_CONNECTOR_KEY:'fx',FLUXER_ORIGIN:`http://127.0.0.1:${server.address().port}`,
      FLUXER_BOT_TOKEN_FILE:'/missing-bot-token', FLUXER_INTERNAL_BEARER:'test-ipc-secret',
      FLUXER_APPROVED_API_HOST:'127.0.0.2',FLUXER_APPROVED_GATEWAY_HOST:'127.0.0.1',
      FLUXER_RPC_PORT:'8769',
      FLUXER_PYTHON_INGRESS_URL:'http://127.0.0.1:8080/internal/fluxer/events',
      FLUXER_ALLOW_INSECURE_LOCAL_DEVELOPMENT:'true',
    },async()=>{
      await assert.rejects(runFluxerNodeService(),/endpoint mismatch/);
    });
  }finally{await new Promise(resolve=>server.close(resolve));}
});
