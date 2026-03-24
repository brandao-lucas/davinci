export async function saveFile(data: string, filename: string): Promise<void> {
  if (typeof window !== 'undefined' && '__TAURI__' in window) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const { save } = await import(/* webpackIgnore: true */ '@tauri-apps/plugin-dialog' as any);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const { writeTextFile } = await import(/* webpackIgnore: true */ '@tauri-apps/plugin-fs' as any);
    const path = await save({ defaultPath: filename });
    if (path) await writeTextFile(path, data);
  } else {
    const blob = new Blob([data], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}

export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI__' in window;
}
