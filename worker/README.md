See [`../README.md`](../README.md) (or [`../README.zh-TW.md`](../README.zh-TW.md)) for what
this project is and how to set it up.

## Local development

```sh
uv venv && uv sync   # editor autocomplete/type hints
npm run dev           # uv run pywrangler dev
```

`ctx.waitUntil()` (used for all deferred webhook processing) does not run correctly under
local dev — anything past the immediate HTTP response needs a real deployment to test.
