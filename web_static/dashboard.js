"use strict";

(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

  function requestId() {
    return window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function postJson(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        "Idempotency-Key": requestId(),
      },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.error?.safe_message || data.error?.code || "request failed");
      error.code = data.error?.code || "request_failed";
      throw error;
    }
    return data;
  }

  async function getJson(path) {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error?.safe_message || data.error?.code || "request failed");
    }
    return data;
  }

  const askForm = document.getElementById("ask-form");
  if (askForm) {
    const question = document.getElementById("ask-question");
    const status = document.getElementById("ask-status");
    const result = document.getElementById("ask-result");
    let requestNumber = 0;

    askForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = question.value.trim();
      if (!value) return;
      requestNumber += 1;
      const activeRequest = requestNumber;
      const submit = askForm.querySelector('button[type="submit"]');
      submit.disabled = true;
      status.dataset.state = "submitting";
      status.textContent = "正在排入 Ask 工作…";
      result.textContent = "";

      try {
        const accepted = await postJson("/api/ask", { question: value });
        status.dataset.taskId = accepted.task_id;
        status.dataset.state = accepted.status || "queued";
        status.textContent = `Ask 工作 ${accepted.status || "queued"}：${accepted.task_id}`;

        for (let attempt = 0; attempt < 120 && activeRequest === requestNumber; attempt += 1) {
          const task = await getJson(`/api/tasks/${encodeURIComponent(accepted.task_id)}`);
          status.dataset.state = task.status;
          status.textContent = `Ask 工作 ${task.status}：${task.task_id}`;
          if (["queued", "running", "cancel_requested"].includes(task.status)) {
            await new Promise((resolve) => window.setTimeout(resolve, 500));
            continue;
          }
          if (task.status === "blocked") {
            result.dataset.state = "blocked";
            result.textContent = "工作遭到預算或政策阻擋；沒有產生回答。";
          } else if (task.status === "unknown") {
            result.dataset.state = "unknown";
            result.textContent = "送出狀態未知。請先核對工作狀態，不要直接重送。";
          } else if (task.status === "succeeded") {
            const citations = task.result?.citations || [];
            if (citations.length === 0) {
              result.dataset.state = "no-evidence";
              result.textContent = "找不到可引用的影片片段；未提供無證據回答。";
            } else {
              result.dataset.state = "answered";
              result.textContent = task.result?.answer || "工作已完成。";
            }
          } else if (task.status === "failed") {
            result.dataset.state = "failed";
            result.textContent = task.safe_error || "Ask 工作失敗。";
          } else {
            result.dataset.state = task.status;
            result.textContent = `Ask 工作狀態：${task.status}`;
          }
          break;
        }
      } catch (error) {
        status.dataset.state = error.code === "budget_blocked" ? "blocked" : "failed";
        status.textContent = error.message;
      } finally {
        if (activeRequest === requestNumber) submit.disabled = false;
      }
    });
  }

  const digestForm = document.getElementById("digest-preview-form");
  if (digestForm) {
    const previewRegion = document.getElementById("digest-preview");
    const status = document.getElementById("digest-status");
    const confirm = document.getElementById("digest-confirm");
    const send = document.getElementById("digest-send");
    let currentPreview = null;

    function updateSendEnabled() {
      send.disabled = !currentPreview || !confirm.checked || currentPreview.counts.selected < 1;
    }

    digestForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const previewButton = digestForm.querySelector('button[type="submit"]');
      previewButton.disabled = true;
      send.disabled = true;
      confirm.checked = false;
      status.dataset.state = "loading";
      status.textContent = "正在組成本機預覽…";
      try {
        const date = document.getElementById("digest-date").value;
        const preview = await postJson("/api/digest/preview", {
          digest_type: document.getElementById("digest-type").value,
          ...(date ? { date } : {}),
        });
        if (preview.remote_calls !== 0) {
          throw new Error("預覽未能確認為本機零外部呼叫；停止確認流程。");
        }
        currentPreview = preview;
        document.getElementById("digest-window").textContent =
          `${preview.window.label} (${preview.window.timezone}) · ${preview.window.start} – ${preview.window.end}`;
        document.getElementById("digest-counts").textContent =
          `候選 ${preview.counts.candidates} · 選入 ${preview.counts.selected} · 待補 ${preview.counts.deferred}`;
        const items = document.getElementById("digest-items");
        items.replaceChildren();
        for (const item of preview.selected_items || []) {
          const li = document.createElement("li");
          li.textContent = `${item.title || item.job} (${item.job})`;
          items.append(li);
        }
        previewRegion.hidden = false;
        status.dataset.state = "previewed";
        status.textContent = `預覽已保存：revision ${preview.revision} · ${preview.digest_id}`;
        updateSendEnabled();
      } catch (error) {
        currentPreview = null;
        previewRegion.hidden = true;
        status.dataset.state = "failed";
        status.textContent = error.message;
      } finally {
        previewButton.disabled = false;
      }
    });

    confirm.addEventListener("change", updateSendEnabled);
    send.addEventListener("click", async () => {
      if (!currentPreview || !confirm.checked || currentPreview.counts.selected < 1) return;
      if (!window.confirm("確認將此版本摘要排入背景寄送佇列？工作者執行後會聯絡設定的寄送服務。")) return;
      send.disabled = true;
      status.dataset.state = "submitting";
      status.textContent = "正在排入摘要寄送工作…";
      try {
        const queued = await postJson("/api/digest/send", {
          digest_id: currentPreview.digest_id,
          revision: currentPreview.revision,
          payload_hash: currentPreview.payload_hash,
        });
        status.dataset.state = queued.status || "queued";
        status.textContent = `已排入背景工作 ${queued.task_id}（${queued.status || "queued"}）；尚未表示已送達。`;
      } catch (error) {
        status.dataset.state = error.code || "failed";
        status.textContent = error.message;
        send.disabled = false;
      }
    });
  }
})();
