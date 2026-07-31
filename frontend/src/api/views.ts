// Client for the saved-views CRUD wrapper (views_api.py, docs/vite-ui-plan.md §6).
import { apiGet, apiSend } from "./client";
import type { ViewTree, ViewDoc, ViewState } from "./types";

export function listViews() {
  return apiGet<{ sections: Record<string, ViewTree> }>("/views");
}
export function loadView(file: string) {
  return apiGet<ViewDoc>(`/views/item/${file}`);
}
export function saveView(name: string, folder: string, state: ViewState) {
  return apiSend<{ file: string }>("PUT", "/views/save", { name, folder, state });
}
export function deleteView(file: string) {
  return apiSend<{ deleted: string }>("DELETE", `/views/item/${file}`);
}
export function makeFolder(parent: string, name: string) {
  return apiSend<{ folder: string }>("POST", "/views/folder", { parent, name });
}
