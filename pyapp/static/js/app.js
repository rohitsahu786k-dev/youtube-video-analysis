// Modal handling
function openIngestModal() {
  const m = document.getElementById("ingestModal");
  if (m) m.classList.add("active");
}

function closeIngestModal() {
  const m = document.getElementById("ingestModal");
  if (m) m.classList.remove("active");
}

async function handleIngestSubmit(event) {
  event.preventDefault();
  const input = document.getElementById("youtubeUrlInput");
  const statusMsg = document.getElementById("ingestStatus");
  const url = input.value.trim();
  if (!url) return;

  statusMsg.innerText = "Queueing ingestion pipeline...";
  statusMsg.style.color = "#38bdf8";

  try {
    const res = await fetch("/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (res.ok) {
      statusMsg.innerText = "Ingestion queued! Redirecting to pipeline...";
      statusMsg.style.color = "#10b981";
      setTimeout(() => {
        window.location.href = "/processing";
      }, 1000);
    } else {
      statusMsg.innerText = data.detail || "Ingestion failed";
      statusMsg.style.color = "#ef4444";
    }
  } catch (err) {
    statusMsg.innerText = "Network error: " + err.message;
    statusMsg.style.color = "#ef4444";
  }
}

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
