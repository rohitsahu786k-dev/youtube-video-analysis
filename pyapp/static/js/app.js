// Legacy chat (Research tools) and retry buttons. Modal / ingest handling lives in ui.js.
// Chat functionality
async function sendChatMessage() {
  const input = document.getElementById("chatInput");
  const box = document.getElementById("chatMessages");
  const q = input.value.trim();
  if (!q) return;

  // Append user bubble
  const userDiv = document.createElement("div");
  userDiv.className = "message-bubble msg-user";
  userDiv.innerText = q;
  box.appendChild(userDiv);
  input.value = "";
  box.scrollTop = box.scrollHeight;

  // Append AI loading bubble
  const aiDiv = document.createElement("div");
  aiDiv.className = "message-bubble msg-ai";
  aiDiv.innerText = "Consulting Gemini AI with analyzed video evidence...";
  box.appendChild(aiDiv);
  box.scrollTop = box.scrollHeight;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q })
    });
    const data = await res.json();
    if (res.ok) {
      aiDiv.textContent = data.answer;
      aiDiv.style.whiteSpace = "pre-wrap";
      if (data.sources && data.sources.length) {
        const sources = document.createElement("div");
        sources.className = "chart-note";
        for (const source of data.sources) {
          const link = document.createElement("a");
          link.textContent = `${source.title} @ ${source.timestamp}s (${source.type}) `;
          const url = new URL(source.url, window.location.origin);
          if (url.protocol === "https:" && url.hostname === "www.youtube.com") link.href = url.href;
          link.target = "_blank"; link.rel = "noopener noreferrer";
          sources.appendChild(link);
        }
        aiDiv.appendChild(sources);
      }
    } else {
      aiDiv.innerText = "Error: " + (data.detail || "Could not generate answer.");
    }
  } catch (err) {
    aiDiv.innerText = "Error: " + err.message;
  }
  box.scrollTop = box.scrollHeight;
}

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-retry-url]");
  if (!button) return;
  button.disabled = true;
  try {
    const response = await fetch(button.dataset.retryUrl, { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Retry failed");
    window.location.reload();
  } catch (error) { button.textContent = error.message; button.disabled = false; }
});
