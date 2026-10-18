# Writing a ChatForge add-on

An add-on is a separate Python package that ChatForge loads at start-up through an **entry point**. It can add
a card to Settings, hear every finished turn and every chat event, send text into the chat as if the user typed
it, and (later) register audio back-ends. ChatForge itself ships no add-on; this is the socket one plugs into.
A voice add-on is the first intended user, which is why the hooks are shaped like a conversation: a turn ends,
the reply streams, the user speaks.

Status: **API version 1**, new in ChatForge 0.2.0, and still allowed to change while the version is 0.x
(`CHANGELOG.md` will say). Source: [`src/chatforge/extensions.py`](../src/chatforge/extensions.py), tests:
[`tests/unit/test_extensions.py`](../tests/unit/test_extensions.py).

## The smallest add-on

```toml
# pyproject.toml of the add-on
[project]
name = "chatforge-hello"
version = "0.1.0"
dependencies = ["chatforge>=0.2"]

[project.entry-points."chatforge.extensions"]
hello = "chatforge_hello:setup"
```

```python
# chatforge_hello/__init__.py
from chatforge.extensions import ExtensionAPI, SettingField, TurnInfo


def setup(api: ExtensionAPI) -> None:
    api.register_settings_section(
        [
            SettingField("shout", "Print replies in capitals", "bool", default=False),
            SettingField("prefix", "Prefix", "text", default=">> "),
        ],
        title="Hello add-on",
        description="Prints each reply to the log.",
    )

    def on_turn(turn: TurnInfo) -> None:
        if turn.ok:
            text = turn.reply_text.upper() if api.settings["shout"] else turn.reply_text
            api.log.info("reply", text=api.settings["prefix"] + text)

    api.on_turn(on_turn)
```

Install it into the same environment as ChatForge (`uv pip install -e .` from the app's folder, for a copy
installed by `Install ChatForge.bat`: `"%LOCALAPPDATA%\Programs\ChatForge\tools\uv.exe" pip install --python
"%LOCALAPPDATA%\Programs\ChatForge\app\.venv\Scripts\python.exe" -e path\to\chatforge-hello`), restart
ChatForge, and Settings > General shows a **Hello add-on** card.

The installer syncs with `uv sync --locked --no-dev --inexact`, so an add-on installed into the app's
environment survives an upgrade (running the installer again). A source install (`uv sync` in a clone) removes
packages that are not in `uv.lock`: use `uv sync --inexact` there, or install the add-on again afterwards.

## What `setup(api)` gets

`setup` is called once per add-on, on ChatForge's core thread, before the window opens. If it raises, the
add-on is skipped (Settings > General lists it as "could not be loaded: <error>") and everything it
registered so far is dropped. A module-level `API_VERSION = 1` on the entry-point object makes ChatForge refuse
an add-on built for another API version instead of loading it half-working.

| Member | What it does |
| --- | --- |
| `api.name`, `api.app_version`, `api.api_version` | Who you are (the entry-point name), ChatForge's version, the API version (1). |
| `api.data_dir` | A folder you may write to: `<ChatForge data folder>/extensions/<name>` (created on first use). |
| `api.log` | A structlog logger named `chatforge.extensions.<name>`; output goes to the app log, secrets redacted. |
| `api.settings` | Your current settings as a dict: registered defaults, overlaid with `config.toml` and the environment. |
| `api.register_settings_section(fields, *, title, section_id="main", description="")` | Adds a card to Settings > General. |
| `api.on_turn(cb)` | `cb(turn: TurnInfo)` after every turn that ends (`chat.done`, or a `chat.error` including a stop). |
| `api.on_event(cb)` | `cb(event: dict)` for every event the chat page hears from the bridge (`chat.start`, `chat.delta`, `chat.tool_call`, `chat.done`, `chat.error`, ...), for example to speak a reply while it streams. |
| `api.on_startup(cb)` / `api.on_shutdown(cb)` | Acquire and release resources. `on_startup` runs right after `setup`; `on_shutdown` on quit, in reverse order of registration. |
| `api.chat.send(text)` | Send `text` as the user's message, the same path as typing. Returns the request id, or `None` if the chat refused it. |
| `api.chat.cancel(request_id)` | Stop that reply (barge-in). The partial reply is kept, as with the Stop button. |
| `api.audio` | Placeholder registries: `register_capture(name, factory)`, `register_playback(name, factory)`, `capture_backends()`, `playback_backends()`, `create_capture(name, ...)`, `create_playback(name, ...)`. ChatForge ships no back-end and defines no frame format: the first add-on with audio decides it, and a second add-on finds it here. |

Callbacks run on the core thread or a bridge worker thread, **never the GUI thread**, so they must return
quickly: hand slow work (a speech model) to your own thread. An exception in a callback is logged and swallowed
(`extension_callback_failed`); it never breaks a turn.

`TurnInfo` is a frozen dataclass: `request_id`, `user_text` (what the user typed; empty for a regenerated turn),
`reply_text` (the final visible text; empty when the turn failed or was stopped), `provider`, `model`, `ok`,
`error_code` (the `chat.error` code, `"cancelled"` for a stop, else `None`).

## Settings

A section is a list of `SettingField(key, label, type, default, choices, minimum, maximum, help)`:

| `type` | Control | Stored as |
| --- | --- | --- |
| `"bool"` | switch | `true` / `false` |
| `"int"`, `"float"` | number box (`minimum`/`maximum` enforced) | number |
| `"text"` | text box | string |
| `"choice"` | drop-down over `choices` | one of the strings |

Keys are lowercase letters, digits and underscores, unique across all your sections. Values live in
`config.toml` under your add-on's name and survive an add-on being absent:

```toml
[extensions.hello]
shout = true
prefix = ">> "
```

and, like every setting, can come from the environment, which wins over the file:
`CHATFORGE_EXTENSIONS__HELLO__SHOUT=1`. ChatForge validates every save against the fields you registered (an
unknown key, a number out of range or a choice that is not offered is refused with the reason) and writes the
file atomically. Never put a secret in a setting: use `keyring` with your own service name, or an environment
variable, as ChatForge does for API keys. Everything an add-on sends to the page is shown as text, never parsed as
markup.

## What an add-on must not assume

- **No GUI access.** There is no window handle, no DOM, no way to add a button to the popup in API version 1.
  A mic button and a status strip are the voice add-on's first request; they will arrive as a version-2 slot, not
  by reaching into the page.
- **One conversation.** ChatForge has one chat; `api.chat.send` appends to it.
- **A small default model.** The starter model has a 4096-token window: an add-on that adds text to the prompt (a
  "speak briefly" instruction) pays for it in every turn.
- **Python 3.12 or 3.13**, the app's own environment, and a possible dependency clash: keep your requirements lean
  (a speech add-on adds `numpy` and `onnxruntime`, around 100 MB).
- **Safe mode.** `CHATFORGE_NO_EXTENSIONS=1` starts ChatForge without loading any add-on; the way to find out
  whether an add-on is the problem.

## Testing an add-on without ChatForge's window

`ExtensionHost` is plain Python. The ChatForge tests build one with fake entry points:

```python
from chatforge.extensions import ExtensionHost

host = ExtensionHost(app_version="0.2.0", get_config=lambda: cfg, home=tmp_path)
host.load([FakeEntryPoint("hello", setup)])  # an object with .name, .load(), .dist
host.dispatch_turn(TurnInfo("req_1", "hi", "hello", "local-npu", "m", True))
```

See `tests/unit/test_extensions.py` for a `FakeEntryPoint` and a complete set of examples.
