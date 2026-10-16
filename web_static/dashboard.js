"use strict";
(() => {
  let token = "", draft = null;
  const notice = document.querySelector("#notice");
  const writes = document.body.dataset.writes === "true";
  const say = text => { notice.textContent = text; };
  async function request(path, payload, key) {
    const options = payload === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "Authorization": `Bearer ${token}`, "Idempotency-Key": key || crypto.randomUUID()}, body: JSON.stringify(payload)};
    const result = await fetch(path, options);
    const value = await result.json();
    if (!result.ok) throw new Error(value.error?.safe_message || value.data?.state || "error");
    return value.data;
  }
  const paragraph = (parent, text) => { const element = document.createElement("p"); element.textContent = text; parent.append(element); return element; };
  async function poll(id, output) {
    for (let attempt = 0; attempt < 120; attempt++) {
      const task = await request(`/api/tasks/${id}`);
      say(`工作 ${id}：${task.status}\n${task.cancellation_note}`);
      if (!["queued", "running", "cancel_requested"].includes(task.status)) {
        showResults(task.result || {state: task.status}, output);
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    say(`工作 ${id} 仍在持久化佇列中；可稍後到工作頁查看。`);
  }
  function showResults(data, output) {
    output.replaceChildren();
    if (data.answer) paragraph(output, data.answer);
    const items = data.items || data.citations || [];
    if (!items.length) paragraph(output, data.status === "accepted" ? "供應商已接受；尚無可靠送達確認。" : data.state || data.status || "沒有結果或可引用的證據。");
    for (const hit of items) {
      const article = document.createElement("article");
      paragraph(article, `${hit.title || hit.job} · ${hit.time_label || "影片層級"}`);
      paragraph(article, hit.snippet || hit.text || "");
      if (hit.source_link) { const a = document.createElement("a"); a.href = hit.source_link; a.textContent = "核對原片"; a.target = "_blank"; a.rel = "noopener noreferrer"; article.append(a); }
      output.append(article);
    }
  }
  document.querySelector("#authorize")?.addEventListener("submit", async event => {
    event.preventDefault(); token = document.querySelector("#token").value; document.querySelector("#token").value = "";
    try { await request("/api/session", {}); say("已授權這次操作。遠端頁面可重新載入以讀取資料。"); } catch (error) { token = ""; say(error.message); }
  });
  document.querySelector("#search-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const query = document.querySelector("#query").value, mode = document.querySelector("#search-mode").value;
    const output = document.querySelector("#search-results"); say("載入中…");
    try {
      if (mode === "fts") { showResults(await request(`/api/search?q=${encodeURIComponent(query)}`), output); say("本機搜尋完成。"); }
      else { if (!writes || !token) throw new Error("此操作需開啟寫入模式，並先授權存取權杖。"); const task = await request(mode === "ask" ? "/api/ask" : "/api/search", {query, mode: mode === "ask" ? "fts" : mode}); await poll(task.task_id, output); }
    } catch (error) { say(error.message); }
  });
  document.querySelector("#digest-preview")?.addEventListener("submit", async event => {
    event.preventDefault(); say("組裝本機預覽中…"); draft = null; document.querySelector("#digest-send").hidden = true;
    try {
      const date = document.querySelector("#digest-date").value;
      draft = await request("/api/digest/preview", {type: document.querySelector("#digest-type").value, new_revision: document.querySelector("#digest-new-revision").checked, ...(date ? {date} : {})});
      const output = document.querySelector("#draft"); output.replaceChildren();
      paragraph(output, `收件者：${draft.recipient || "未設定"} · ${draft.provider} · 第 ${draft.revision} 版`);
      paragraph(output, `${draft.payload.window_start} 至 ${draft.payload.window_end} · ${draft.timezone}`);
      paragraph(output, `候選 ${draft.payload.candidate_count} · 選入 ${draft.payload.selected_count} · 待補 ${draft.payload.deferred_count} · 含已即時通知項目：${draft.includes_immediate_notifications}`);
      for (const item of draft.payload.selected_items) paragraph(output, `${item.title}\n${item.summary}`);
      document.querySelector("#digest-send").hidden = !(writes && draft.recipient && draft.payload.selected_count);
      say("預覽完成，尚未寄送。");
    } catch (error) { say(error.message); }
  });
  document.querySelector("#digest-send")?.addEventListener("click", async event => {
    if (!draft) return; const button = event.currentTarget; button.disabled = true;
    try { const task = await request("/api/digest/send", {digest_id: draft.digest_id, revision: draft.revision, payload_hash: draft.payload_hash}, `digest-${draft.digest_id}-${draft.revision}`); await poll(task.task_id, document.querySelector("#draft")); }
    catch (error) { say(error.message); } finally { button.disabled = false; }
  });
  document.querySelector("#load-notifications")?.addEventListener("click", async () => {
    try { const value = await request("/api/notifications"); const output = document.querySelector("#notifications"); output.replaceChildren(); for (const item of value.items) paragraph(output, `${item.delivery_id} · ${item.status === "accepted" ? "供應商已接受" : item.status}`); if (!value.items.length) paragraph(output, "目前沒有通知紀錄。"); } catch (error) { say(error.message); }
  });
  for (const button of document.querySelectorAll(".retry-rss")) button.addEventListener("click", async () => {
    try { const task = await request(`/api/jobs/${encodeURIComponent(button.dataset.job)}/retry`, {allow_cloud: true}); say(`已排入重排工作 ${task.task_id}；原 RSS worker 負責後續處理。`); } catch (error) { say(error.message); }
  });
  for (const button of document.querySelectorAll(".cancel-task")) button.addEventListener("click", async () => {
    try { const task = await request(`/api/tasks/${button.dataset.task}/cancel`, {}); say(`${task.status}：${task.cancellation_note}`); button.disabled = true; } catch (error) { say(error.message); }
  });
  const pagination = document.querySelector(".pagination");
  if (pagination) {
    const page = Number(pagination.dataset.page), size = Number(pagination.dataset.size), total = Number(pagination.dataset.total);
    for (const [id, target, disabled] of [["previous-page", page - 1, page <= 1], ["next-page", page + 1, page * size >= total]]) {
      const button = document.getElementById(id); button.disabled = disabled;
      button.addEventListener("click", () => { const url = new URL(location.href); url.searchParams.set("page", target); location.assign(url); });
    }
  }
})();
