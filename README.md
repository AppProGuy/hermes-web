# Hermes Web

A compact, dark, local-first web interface for [Hermes Agent](https://hermes-agent.nousresearch.com). It is designed to make Hermes feel like a fast personal chat agent while preserving visibility into tool use and sensitive actions.

## What changed

- Fully English, high-density dark interface
- Streaming responses, reasoning, and expandable tool cards
- Inline image, audio, and video results from approved media roots
- Browser dictation with `SpeechRecognition`
- Automatic playback attempt for Hermes `text_to_speech` results
- Native Approve / Cancel cards for Hermes dangerous-command and clarification callbacks
- Conversation persistence and responsive mobile layout
- Optional bearer-token protection for API and WebSocket access
- Two runtime modes:
  - **Local mode:** imports `AIAgent` and runs Hermes in-process
  - **Proxy mode:** serves this interface on one computer while forwarding to an existing `hermes-web` host

The interaction model and safety presentation were informed by [OpenMausBot](https://github.com/milind-soni/OpenMausBot). This is an independent Hermes-specific implementation; it does not embed OpenMausBot.

## Mac Studio → Mac Pro quick start

The supplied `start-web.sh` automatically uses proxy mode when Hermes is not installed locally. Its default upstream is the Mac Pro at `http://100.92.91.49:3005`.

```bash
cd hermes-web
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start-web.sh
```

Open `http://127.0.0.1:3005` on the Mac Studio. Other Tailscale devices can use the Mac Studio's Tailscale address if the firewall allows it.

Proxy mode works immediately with the older Mac Pro server for chat, history, streaming, and tool cards. Approval prompts and remote filesystem media require this upgraded backend to be installed on the Mac Pro, because the older protocol does not expose those capabilities.

## Run directly on the Mac Pro

Copy or clone this fork on the Mac Pro, then point it to the Hermes checkout if auto-detection does not find it:

```bash
export HERMES_AGENT_HOME="$HOME/hermes-agent"
export HERMES_WEB_TOKEN="replace-with-a-long-random-token"
./start-web.sh
```

The backend searches these locations in order:

1. `$HERMES_AGENT_HOME`
2. `~/hermes-agent`
3. `~/.hermes/hermes-agent`

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `HERMES_REMOTE_URL` | Enables proxy mode and selects the upstream | Auto-selects the Mac Pro when Hermes is absent |
| `HERMES_REMOTE_TOKEN` | Token used by the proxy when the upgraded remote requires one | Empty |
| `HERMES_WEB_TOKEN` | Protects this server's APIs and WebSocket | Empty |
| `HERMES_AGENT_HOME` | Hermes Agent source directory for local mode | Auto-detected |
| `HERMES_WEB_HOST` | Bind address | `0.0.0.0` |
| `HERMES_WEB_PORT` | Listen port | `3005` |
| `HERMES_MEDIA_ROOTS` | Colon-separated directories allowed for inline media | `~/.hermes` |
| `HERMES_APPROVAL_TIMEOUT` | Seconds to wait for an approval response | `300` |
| `HERMES_WEB_DATA_DIR` | Conversation storage directory | `~/.hermes/hermes-web` |

If `HERMES_WEB_TOKEN` is set, enter the same value once in the interface's Settings panel. The browser stores it in local storage and never sends it as a chat message.

## Voice notes

Browser dictation depends on the browser's `SpeechRecognition` implementation. Most browsers require a secure context for microphone access. Localhost works over HTTP; remote Tailscale access should use HTTPS, for example through Tailscale Serve. Generated TTS audio remains available as a normal audio player if autoplay is blocked.

## Security boundaries

- Keep this service on localhost or a trusted Tailscale network; do not expose port `3005` directly to the public internet.
- Set `HERMES_WEB_TOKEN` whenever more than one trusted user can reach the host.
- Media files are only served from `HERMES_MEDIA_ROOTS` and only when their MIME type is image, audio, or video.
- Tool approvals use Hermes' native approval callback. The interface does not label a tool as approved after it has already executed.
- API keys remain server-side and are removed from configuration events sent to the upgraded browser client.

## Architecture

```text
Browser ── HTTP/WebSocket ── Hermes Web backend
                                  ├── local mode: AIAgent in-process
                                  └── proxy mode: remote Hermes Web host
```

Conversations are stored as JSON under `~/.hermes/hermes-web/conversations/` in local mode. Proxy mode leaves persistence on the remote Hermes host.

## Development checks

```bash
python3 -m py_compile backend.py
sed -n '/<script>/,/<\/script>/p' frontend/index.html | sed '1d;$d' | node --check
python3 -m unittest discover -s tests -v
```

