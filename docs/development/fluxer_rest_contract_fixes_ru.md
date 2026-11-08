# Исправление REST-контрактов Fluxer после независимой проверки

Документ фиксирует причины FLX-LIVE-01–04, исправления и границу локальной
проверки. Исходный отчёт и evidence переданы пользователем; их live-результаты
не являются новым запуском исправленного кода на сервере.

## Исходное состояние и подтверждение причин

База: `04b3b4186839fc099a60b71a28fef4513f136da3` (CP001–CP041), дерево
`764fab2bbabd27cce44ec704004979a50c61417c`. CP042 в переданную историю не входит.
Исправление оформлено отдельной веткой `fix/fluxer-rest-message-contracts`.

Независимая VM использовала Fluxer `fluxer-release@2026.1106.212644` и
`@fluxerjs/core` 3.3.0. Тег Fluxer соответствует исходникам
`573afe248c9d1584557567709c68cfcdc7228dbb`. В этих исходниках
`packages/schema/src/domains/message/MessageResponseSchemas.ts` определяет
Message без обязательного guild_id и с nullish webhook_id. SDK Message
конструирует `guildId = data.guild_id ?? null` и
`webhookId = data.webhook_id ?? null`. Это согласуется с переданными
`live-write-readbacks.json` и `live-sdk-raw-*.json`.

Первичный источник: [MessageResponseSchemas.ts на проверенном commit](https://github.com/fluxerapp/fluxer/blob/573afe248c9d1584557567709c68cfcdc7228dbb/packages/schema/src/domains/message/MessageResponseSchemas.ts).

| Находка | Подтверждённая причина | Исправление |
| --- | --- | --- |
| FLX-LIVE-01 | После одного успешного SDK send проверка требовала message.guildId, хотя SDK возвращает null. | Guild устанавливается по свежему container, сообщение должно содержать его точный ID. Явный чужой guild отвергается. |
| FLX-LIVE-02 | Сырой embedded starter обязан был содержать guild_id и webhook_id:null. | Guild/parent подтверждаются самим thread, starter проверяется по собственным ID, container, тексту и автору. Отсутствующий/null webhook_id допустим по полной серверной схеме; webhook не принимается. |
| FLX-LIVE-03 | GET настоящего родителя reply возвращал SDK guildId:null и отвергался до отправки. | Проверяется точный parent ID и непосредственный container в уже подтверждённом guild. |
| FLX-LIVE-04 | Собственная копия отвергалась перед edit/delete из-за SDK guildId:null. | И исходный Message, и результат edit проверяются в свежем container; bot identity, ACL и webhookId:null остаются обязательными. |
| Дополнительная проверка кода | fetch_message имел то же обязательное сравнение guildId и блокировал чтение такой копии для Python ownership/preflight. | Применено то же правило container/guild, без ослабления author, timestamp, attachment и webhook-проверок. |

`message_scope.ts` содержит одно правило для полного SDK Message. Только null
или совпадающий guild допустимы; отсутствующая SDK property undefined не
принимается. Для сырого forum starter отсутствие необязательного поля
нормализуется на границе полного REST-ответа. Это не правило для частичных
Gateway-событий или произвольных неполных provider DTO.

Thread ID и starter ID сохраняются независимо из ответа сервера. Они могут
совпадать; отсутствие starter не заменяется thread ID. В forum receipt также
добавлена проверка точного текста starter: усечённый или подменённый результат
после POST остаётся unknown_delivery.

## Регрессионные сценарии

Новый `fluxer-connector/tests/message_rest_contracts.test.mjs` использует
настоящие SDK Client, ChannelManager, MessageManager и Message, а также
настоящий authenticated HTTP RPC. Подменены только удалённые REST-эффекты и
guild ACL provider; состояние удалённых сообщений хранится независимо.
Это contract regression, а не эмуляция успешной live-приёмки.

До исправления, на отдельном чистом worktree CP041: 5 положительных сценариев
FAIL, 3 отрицательных PASS. После: все 8 PASS. Проверяются отдельный read,
send/read, reply к пользователю, собственные edit/delete,
forum starter с равным thread ID и вложенный reply. Противоречивые guild,
container, автор, webhook и текст не получают положительную квитанцию;
после postwrite-ошибки остаётся ровно одна попытка и физический объект.

Межпроцессные Python/Node-сценарии запускаются для двух форматов: отдельно
заданные thread/starter ID с заполненным guild и измеренный omission-формат с
равными ID. Сохраняются проверки точной квитанции, readback, ownership,
повторного attempt без второго POST и потерянного ответа без повтора.
SQLite mutation-сценарии теперь используют SDK guildId:null и проверяют
applied_revision, mutation_state, readback и отказ изменения чужой копии.

## Проверки текущего изменения

- Node 22.16.0: npm ci, check:lock, TypeScript — PASS; 117/117 Node tests.
- Contract regression на CP041: 5 FAIL / 3 PASS; на исправлении: 8/8 PASS.
- Два Python/Node crossprocess-модуля: 14 passed, 2 subtests.
- Полный pytest (Python 3.12.14): 2201 passed, 1 failed, 1 skipped,
  98 subtests. Единственный failure — существовавшие в CP041 устаревшие
  локальные AP URL в двух Fluxer-фикстурах. Воспроизведён отдельно на чистом
  CP041; будет исправлен отдельным тестовым патчем, без изменения scanner.
- Skip: opt-in actual Compose verifier без тестового env/Discord token.
- Fedify: TypeScript и 20 security tests PASS. Штатный npm test BLOCKED
  запретом Unix socket у tsx CLI. Альтернативный node --import tsx прошёл
  51/52 script files; verify-env-loading запускает тот же запрещённый child
  CLI. Это ограничение окружения, не успешный полный npm test.
- uv lock --check и git diff --check — PASS. Docker CLI здесь отсутствует.

Первый полный pytest с унаследованным SOCKS proxy остановлен после
воспроизведения httpx ImportError (socksio отсутствует). Приведённые выше
результаты получены без навязанных proxy-переменных. Эта настройка среды
не включена в production-код или lockfiles.

## Открытые проверки и ограничения

Исправленный код здесь не запускался против настоящего Fluxer-инстанса.
Нужен повтор на том же pinned release: send, forum Create, reply к пользователю
и внутри forum thread, readback, bot-owned edit/delete. Затем проверить
реальную SQLite доставку parent → сохранённая квитанция → разблокировка child
→ scheduler → квитанция child с точным parent, потерю ACK после POST и shutdown
под нагрузкой. Существующие локальные scheduler-тесты сохраняются; они не
заменяют этот live-сценарий.

Полный Compose verifier с отдельным Discord test token, Gateway replay и
остальные открытые acceptance gates из входного отчёта остаются незавершёнными.
Изменения не подключают записи в main.ts и не повышают capabilities.
Lifetime/deployment locks, schema, scheduler, CreateAttempt/CAS и политика
unknown_delivery не меняются. В Python production изменена только docstring.

В переданном Fedify audit отдельно отмечены 6 advisory, включая 1 high.
Они не исправляются этим патчем REST-контрактов: зависимости Fedify не менялись.
Факт наличия advisory не доказывает достижимость каждого уязвимого пути в
bridge; отдельная оценка и обновление зависимостей остаются открытыми.

## Повторная проверка

Использовать Node 22 и Python >=3.12. Из корня проекта:

```sh
uv sync --frozen --extra dev
uv lock --check
.venv/bin/python -m pytest -q
cd fluxer-connector
npm ci
npm run check:lock
npm run check
npm test
```

В тестовом окружении не должно быть неработающего SOCKS proxy для httpx.
Если такие переменные навязаны средой, запускать локальные pytest-сценарии
без ALL_PROXY/all_proxy, HTTP_PROXY/http_proxy и HTTPS_PROXY/https_proxy.
Это настройка конкретного окружения, а не изменение зависимостей проекта.

Bundle поставляется с полной историей ветки и ref тега v0.1.0, чтобы следующая
проверка не повторила блокер исторических тестов из переданного HEAD-архива.
