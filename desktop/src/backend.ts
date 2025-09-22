import { spawn, ChildProcessWithoutNullStreams } from 'child_process';
import path from 'path';
import { existsSync } from 'fs';

let backendProcess: ChildProcessWithoutNullStreams | null = null;

const PYTHON_BINARIES = [
  path.join(process.cwd(), '..', '.venv', 'bin', 'python'),
  path.join(process.cwd(), '..', '.venv', 'bin', 'python3'),
  'python3',
  'python'
];

function resolvePython(): string {
  for (const candidate of PYTHON_BINARIES) {
    if (candidate.startsWith('python')) {
      return candidate;
    }
    if (existsSync(candidate)) {
      return candidate;
    }
  }
  return 'python3';
}

export async function initializeBackend(): Promise<void> {
  if (backendProcess) {
    return;
  }

  const python = resolvePython();
  const appPath = path.join(process.cwd(), '..', 'app.py');

  backendProcess = spawn(python, [appPath], {
    cwd: path.join(process.cwd(), '..'),
    env: {
      ...process.env,
      NODE_ENV: process.env.NODE_ENV || 'production',
    },
    stdio: ['ignore', 'pipe', 'pipe']
  });

  backendProcess.stdout.on('data', (data) => {
    process.stdout.write(`[backend] ${data}`);
  });

  backendProcess.stderr.on('data', (data) => {
    process.stderr.write(`[backend] ${data}`);
  });

  backendProcess.on('exit', (code, signal) => {
    console.warn(`Backend exited (code=${code}, signal=${signal})`);
    backendProcess = null;
  });
}

export function shutdownBackend(): void {
  if (!backendProcess) return;
  backendProcess.kill();
  backendProcess = null;
}
