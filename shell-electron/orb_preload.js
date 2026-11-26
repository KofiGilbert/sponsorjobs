/* The Orb page's bridge (shell-electron/orb_view.js). It can only say what the Orb did: its
 * size, a drag, a click or an answer the person gave in its bubble. The app page decides what
 * each one means; nothing here reaches the pane, the engine or the file system. */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("orbShell", {
  onUpdate: (cb) => ipcRenderer.on("orb:update", (_e, data) => cb(data)),
  size: (w, h) => ipcRenderer.send("orb:size", { w, h }),
  drag: (dx, dy) => ipcRenderer.send("orb:drag", { dx, dy }),
  dragEnd: () => ipcRenderer.send("orb:drag-end"),
  move: (dx, dy) => ipcRenderer.send("orb:move", { dx, dy }),
  reset: () => ipcRenderer.send("orb:reset"),
  // { kind: "open" | "act" | "menu" | "ready", ... } for the app page
  event: (data) => ipcRenderer.send("orb:event", data),
});
