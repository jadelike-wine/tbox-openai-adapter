const STORAGE_KEY = "tbox-adapter-playground-state";

const state = {
  messages: [],
  pendingFiles: [],
  abortController: null,
};

const els = {
  apiFormat: document.getElementById("apiFormat"),
  apiKey: document.getElementById("apiKey"),
  model: document.getElementById("model"),
  userId: document.getElementById("userId"),
  systemPrompt: document.getElementById("systemPrompt"),
  streamMode: document.getElementById("streamMode"),
  loadModelsBtn: document.getElementById("loadModelsBtn"),
  modelList: document.getElementById("modelList"),
  fileInput: document.getElementById("fileInput"),
  uploadBtn: document.getElementById("uploadBtn"),
  uploadStatus: document.getElementById("uploadStatus"),
  attachmentList: document.getElementById("attachmentList"),
  pendingAttachments: document.getElementById("pendingAttachments"),
  chatTitle: document.getElementById("chatTitle"),
  statusBar: document.getElementById("statusBar"),
  messageList: document.getElementById("messageList"),
  composer: document.getElementById("composer"),
  promptInput: document.getElementById("promptInput"),
  sendBtn: document.getElementById("sendBtn"),
  stopBtn: document.getElementById("stopBtn"),
  clearBtn: document.getElementById("clearBtn"),
  messageTemplate: document.getElementById("messageTemplate"),
};

function restoreInputs() {
  const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  els.apiFormat.value = saved.apiFormat || "openai";
  els.apiKey.value = saved.apiKey || "";
  els.model.value = saved.model || "tbox-codex";
  els.userId.value = saved.userId || "demo-user";
  els.systemPrompt.value = saved.systemPrompt || "";
  els.streamMode.checked = saved.streamMode ?? true;
  syncTitle();
}

function persistInputs() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      apiFormat: els.apiFormat.value,
      apiKey: els.apiKey.value,
      model: els.model.value,
      userId: els.userId.value,
      systemPrompt: els.systemPrompt.value,
      streamMode: els.streamMode.checked,
    }),
  );
}

function syncTitle() {
  els.chatTitle.textContent =
    els.apiFormat.value === "openai"
      ? "OpenAI chat.completions"
      : "Anthropic messages";
}

function setStatus(message, tone = "info") {
  els.statusBar.textContent = message;
  if (tone === "error") {
    els.statusBar.style.background = "rgba(180, 35, 24, 0.12)";
    els.statusBar.style.color = "#8f2117";
  } else if (tone === "success") {
    els.statusBar.style.background = "rgba(11, 110, 79, 0.12)";
    els.statusBar.style.color = "#084c39";
  } else {
    els.statusBar.style.background = "rgba(217, 119, 6, 0.12)";
    els.statusBar.style.color = "#7c4a03";
  }
}

function authHeaders(isJson = true) {
  const headers = {};
  const apiKey = els.apiKey.value.trim();
  if (apiKey) {
    headers.Authorization = `Bearer ${apiKey}`;
  }
  if (isJson) {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

function createMessage(role, content, files = [], kind = "") {
  const node = els.messageTemplate.content.firstElementChild.cloneNode(true);
  node.classList.add(role);
  if (kind) node.classList.add(kind);
  node.querySelector(".message-role").textContent = role;
  node.querySelector(".message-body").textContent = content;

  const filesNode = node.querySelector(".message-files");
  files.forEach((file) => {
    const chip = document.createElement("span");
    chip.className = "file-chip";
    chip.textContent = `${file.name} (${file.id})`;
    filesNode.appendChild(chip);
  });

  return node;
}

function appendRenderedMessage(role, content, files = [], kind = "") {
  const node = createMessage(role, content, files, kind);
  els.messageList.appendChild(node);
  els.messageList.scrollTop = els.messageList.scrollHeight;
  return node;
}

function updatePendingFiles() {
  els.attachmentList.innerHTML = "";
  els.pendingAttachments.innerHTML = "";

  if (!state.pendingFiles.length) {
    els.attachmentList.innerHTML = '<span class="empty-chip">No pending attachments</span>';
    return;
  }

  state.pendingFiles.forEach((file, index) => {
    const cardChip = document.createElement("span");
    cardChip.className = "file-chip";
    cardChip.innerHTML = `<span>${file.name} <small>${file.id}</small></span>`;

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      state.pendingFiles.splice(index, 1);
      updatePendingFiles();
    });
    cardChip.appendChild(removeBtn);
    els.attachmentList.appendChild(cardChip);

    const inlineChip = document.createElement("span");
    inlineChip.className = "file-chip";
    inlineChip.textContent = file.name;
    els.pendingAttachments.appendChild(inlineChip);
  });
}

function resetComposerAfterSend() {
  els.promptInput.value = "";
  state.pendingFiles = [];
  updatePendingFiles();
}

function toggleBusy(busy) {
  els.sendBtn.disabled = busy;
  els.stopBtn.disabled = !busy;
  els.uploadBtn.disabled = busy;
}

function fileKindFromName(name) {
  const value = name.toLowerCase();
  if (/\.(png|jpe?g|gif|webp|bmp|svg)$/.test(value)) return "IMAGE";
  if (/\.(mp3|wav|m4a|aac|ogg)$/.test(value)) return "AUDIO";
  if (/\.(mp4|mov|avi|mkv|webm)$/.test(value)) return "VIDEO";
  return "FILE";
}

async function loadModels() {
  setStatus("Loading models...");
  try {
    const response = await fetch("/openai/v1/models", {
      headers: authHeaders(false),
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body?.error?.message || "Failed to load models");
    }
    els.modelList.innerHTML = "";
    body.data.forEach((item) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "model-chip secondary-btn";
      chip.textContent = item.id;
      chip.addEventListener("click", () => {
        els.model.value = item.id;
        persistInputs();
        setStatus(`Model selected: ${item.id}`, "success");
      });
      els.modelList.appendChild(chip);
    });
    setStatus(`Loaded ${body.data.length} model(s).`, "success");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function uploadFiles() {
  const files = Array.from(els.fileInput.files || []);
  if (!files.length) {
    setStatus("Select at least one file before uploading.", "error");
    return;
  }

  toggleBusy(true);
  setStatus(`Uploading ${files.length} file(s)...`);
  els.uploadStatus.textContent = "Uploading...";

  try {
    for (const file of files) {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/openai/v1/files", {
        method: "POST",
        headers: authHeaders(false),
        body: form,
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(body?.error?.message || `Upload failed for ${file.name}`);
      }
      state.pendingFiles.push({
        id: body.data,
        name: file.name,
        kind: fileKindFromName(file.name),
      });
    }
    els.fileInput.value = "";
    updatePendingFiles();
    els.uploadStatus.textContent = `${state.pendingFiles.length} file(s) ready to attach to the next message.`;
    setStatus("Files uploaded.", "success");
  } catch (error) {
    els.uploadStatus.textContent = error.message;
    setStatus(error.message, "error");
  } finally {
    toggleBusy(false);
  }
}

function buildOpenAIPayload(prompt) {
  const messages = [];
  const systemPrompt = els.systemPrompt.value.trim();
  if (systemPrompt) {
    messages.push({ role: "system", content: systemPrompt });
  }
  state.messages.forEach((message) => {
    messages.push({ role: message.role, content: message.content });
  });
  messages.push({ role: "user", content: prompt });

  return {
    model: els.model.value.trim(),
    user: els.userId.value.trim(),
    stream: els.streamMode.checked,
    messages,
    files: state.pendingFiles.map((file) => ({
      type: file.kind,
      fileId: file.id,
    })),
  };
}

function buildAnthropicPayload(prompt) {
  const messages = state.messages.map((message) => ({
    role: message.role,
    content: message.content,
  }));

  const contentBlocks = [{ type: "text", text: prompt }];
  state.pendingFiles.forEach((file) => {
    contentBlocks.push({
      type: "file",
      source: { type: "file", file_id: file.id },
      file_kind: file.kind,
    });
  });
  messages.push({
    role: "user",
    content: state.pendingFiles.length ? contentBlocks : prompt,
  });

  return {
    model: els.model.value.trim(),
    system: els.systemPrompt.value.trim() || undefined,
    max_tokens: 1024,
    stream: els.streamMode.checked,
    metadata: { user_id: els.userId.value.trim() },
    messages,
  };
}

async function sendMessage(event) {
  event.preventDefault();
  persistInputs();

  const prompt = els.promptInput.value.trim();
  if (!prompt) {
    setStatus("Prompt cannot be empty.", "error");
    return;
  }

  const userFiles = [...state.pendingFiles];
  const apiFormat = els.apiFormat.value;
  const endpoint =
    apiFormat === "openai" ? "/openai/v1/chat/completions" : "/anthropic/v1/messages";
  const payload =
    apiFormat === "openai" ? buildOpenAIPayload(prompt) : buildAnthropicPayload(prompt);

  appendRenderedMessage("user", prompt, userFiles);

  const assistantNode = appendRenderedMessage("assistant", "");
  const assistantBody = assistantNode.querySelector(".message-body");

  toggleBusy(true);
  setStatus("Waiting for response...");

  try {
    state.abortController = new AbortController();
    let assistantText = "";
    if (els.streamMode.checked) {
      assistantText = await streamRequest(endpoint, payload, apiFormat, assistantBody);
    } else {
      assistantText = await jsonRequest(endpoint, payload, apiFormat);
      assistantBody.textContent = assistantText;
    }

    state.messages.push({ role: "user", content: prompt, files: userFiles });
    state.messages.push({ role: "assistant", content: assistantText });
    resetComposerAfterSend();
    setStatus("Response complete.", "success");
  } catch (error) {
    if (error.name === "AbortError") {
      const partialText = assistantBody.textContent.trim();
      if (partialText) {
        state.messages.push({ role: "user", content: prompt, files: userFiles });
        state.messages.push({ role: "assistant", content: partialText });
        resetComposerAfterSend();
        setStatus("Streaming stopped. Partial output kept.");
      } else {
        assistantNode.remove();
        setStatus("Streaming stopped.");
      }
      return;
    }
    assistantNode.remove();
    appendRenderedMessage("error", error.message, [], "error");
    setStatus(error.message, "error");
  } finally {
    state.abortController = null;
    toggleBusy(false);
  }
}

async function jsonRequest(endpoint, payload, apiFormat) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: authHeaders(true),
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body?.error?.message || "Request failed");
  }
  return apiFormat === "openai"
    ? body.choices?.[0]?.message?.content || ""
    : body.content?.map((item) => item.text).join("") || "";
}

async function streamRequest(endpoint, payload, apiFormat, bodyNode) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: authHeaders(true),
    body: JSON.stringify(payload),
    signal: state.abortController.signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body?.error?.message || "Streaming request failed");
  }
  if (!response.body) {
    throw new Error("Streaming response body is unavailable");
  }

  return apiFormat === "openai"
    ? parseOpenAIStream(response.body, bodyNode)
    : parseAnthropicStream(response.body, bodyNode);
}

async function parseOpenAIStream(stream, bodyNode) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    while (buffer.includes("\n\n")) {
      const boundary = buffer.indexOf("\n\n");
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      const dataLine = rawEvent.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) continue;

      const data = dataLine.slice(5).trim();
      if (data === "[DONE]") return text;

      const json = JSON.parse(data);
      const delta = json.choices?.[0]?.delta?.content || "";
      if (delta) {
        text += delta;
        bodyNode.textContent = text;
      }
    }
  }

  return text;
}

async function parseAnthropicStream(stream, bodyNode) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    while (buffer.includes("\n\n")) {
      const boundary = buffer.indexOf("\n\n");
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);

      let eventName = "";
      let data = "";
      rawEvent.split("\n").forEach((line) => {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
      });

      if (!data) continue;
      if (data === "[DONE]" || eventName === "message_stop") return text;

      const json = JSON.parse(data);
      if (eventName === "content_block_delta" && json.delta?.text) {
        text += json.delta.text;
        bodyNode.textContent = text;
      }
    }
  }

  return text;
}

function clearConversation() {
  state.messages = [];
  state.pendingFiles = [];
  els.messageList.innerHTML = "";
  updatePendingFiles();
  setStatus("Conversation cleared.");
}

function stopStreaming() {
  if (state.abortController) {
    state.abortController.abort();
    setStatus("Streaming stopped.");
  }
}

function wireEvents() {
  [
    els.apiFormat,
    els.apiKey,
    els.model,
    els.userId,
    els.systemPrompt,
    els.streamMode,
  ].forEach((element) => {
    element.addEventListener("change", () => {
      syncTitle();
      persistInputs();
    });
    element.addEventListener("input", persistInputs);
  });

  els.loadModelsBtn.addEventListener("click", loadModels);
  els.uploadBtn.addEventListener("click", uploadFiles);
  els.composer.addEventListener("submit", sendMessage);
  els.clearBtn.addEventListener("click", clearConversation);
  els.stopBtn.addEventListener("click", stopStreaming);
}

restoreInputs();
updatePendingFiles();
wireEvents();
