# iMessage AI Desktop (NodeGUI)

This directory now contains a pure NodeGUI desktop shell for the iMessage AI
agent—no Svelte layer required.

## Requirements

- Node.js 18+
- `npm` or `yarn`
- Python backend set up via `make setup` and configured with `OPENAI_API_KEY`,
  `IMSG_AI_TOKEN`, `IMSG_AI_SECRET`.

## Getting Started

```bash
cd desktop
npm install
npm run dev
```

`npm run dev` launches the NodeGUI app via `ts-node-dev`. The desktop shell spawns
`python app.py` automatically (using `.venv/bin/python` if available) and connects
to the backend at `http://127.0.0.1:5000` via REST and Socket.IO.

Build for distribution:

```bash
npm run build
npm run start
```

`npm run build` transpiles the TypeScript sources into `dist/`. Package the output
with the `@nodegui/qode` bundler (or similar) to produce a `.app` for macOS.

## Project structure

```
desktop/
  src/
    main.ts        # NodeGUI bootstrap
    ui.ts          # UI layout + interactions
    api.ts         # REST/Socket.IO client wrapper
    backend.ts     # Spawns/stops the Python backend
  package.json     # Dependencies and scripts
  tsconfig.json    # TypeScript configuration
```

## Features

- View live message feed (incoming, sent, AI responses).
- Send bulk messages to multiple numbers.
- Schedule daily messages with cancel support.
- Refresh and inspect current backend settings.

## Next steps

- Integrate macOS Keychain for storing the auth token securely.
- Add backend process watchdog / restart controls.
- Package as a `.app` with launchd integration for background behavior.
