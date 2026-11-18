// Invoke npm's CLI or the Windows Desktop carrier without a command shell.
import { access } from 'node:fs/promises'
import path from 'node:path'

export async function dshCommand({ cli, env = process.env } = {}) {
  if (env.DSH_CLI_ENTRY) return {
    command: env.DSH_CLI_EXECUTABLE || process.execPath,
    args: ['--expose-internals', env.DSH_CLI_ENTRY],
    env: { ...env, ...(env.DSH_CLI_EXECUTABLE ? { ELECTRON_RUN_AS_NODE: '1' } : {}) },
  }
  const selected = cli || (process.platform === 'win32'
    ? path.join(env.LOCALAPPDATA || '', 'Programs', 'DeepSeek Harness', 'resources', 'runtime', 'cli', 'bin', 'dsh.cmd')
    : 'dsh')
  if (/\.cmd$/i.test(selected)) {
    // These are the two paths used by the shipped dsh.cmd. Executing them
    // directly also handles spaces without cmd.exe interpolation.
    const bin = path.dirname(path.resolve(selected))
    const command = path.resolve(bin, '../../../..', 'DeepSeek Harness.exe')
    const entry = path.resolve(bin, '../../..', 'app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js')
    await access(selected)
    await access(command)
    return { command, args: ['--expose-internals', entry], env: { ...env, ELECTRON_RUN_AS_NODE: '1' } }
  }
  return { command: selected, args: [], env: { ...env } }
}
