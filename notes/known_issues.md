# Known issues

- Verified: bridge-local bans do not remove existing remote subscriber rows and do not emit ActivityPub moderation activities.
- Verified: remote handle resolution remains string-only; WebFinger/actor fetch resolution is not implemented.
- Known operational behavior: a globally banned super-admin cannot unban themselves through Discord commands; recovery requires another non-banned super-admin or direct database intervention.
