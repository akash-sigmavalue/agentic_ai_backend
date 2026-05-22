from __future__ import annotations

"""Browser-facing HTML route."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse)
async def root():
    """Serve a minimal browser interface for local API testing."""
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>DuckDuckGo Search Agent</title>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <style>
            body {
                margin: 0;
                background: #343541;
                color: #ececf1;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                height: 100vh;
                display: flex;
                flex-direction: column;
            }
            header {
                border-bottom: 1px solid #4d4d4f;
                font-weight: 600;
                padding: 16px 20px;
                text-align: center;
            }
            main {
                flex: 1;
                overflow-y: auto;
            }
            .message {
                display: flex;
                justify-content: center;
                padding: 24px 20px;
            }
            .assistant {
                background: #444654;
            }
            .content {
                max-width: 820px;
                width: 100%;
                line-height: 1.6;
            }
            form {
                border-top: 1px solid #4d4d4f;
                display: flex;
                gap: 10px;
                padding: 24px 20px;
            }
            input {
                background: #40414f;
                border: 1px solid #565869;
                border-radius: 8px;
                color: #fff;
                flex: 1;
                font: inherit;
                padding: 14px 16px;
            }
            button {
                background: #19c37d;
                border: 0;
                border-radius: 8px;
                color: #fff;
                cursor: pointer;
                font: inherit;
                font-weight: 600;
                padding: 0 18px;
            }
            a {
                color: #19c37d;
            }
            .status {
                color: #b4b4c3;
                font-style: italic;
            }
            .sources {
                border: 1px solid #4d4d4f;
                border-radius: 8px;
                margin-top: 24px;
                padding: 14px;
            }
            .source {
                margin-bottom: 12px;
            }
            .url {
                color: #b4b4c3;
                font-size: 12px;
            }
        </style>
    </head>
    <body>
        <header>Search & Extract Agent</header>
        <main id="chat">
            <section class="message assistant">
                <div class="content">
                    Ask me anything. I can search the live web, read sources, and stream an answer back here.
                </div>
            </section>
        </main>
        <form id="form">
            <input id="query" type="text" placeholder="Send a message..." autocomplete="off" required>
            <button type="submit">Send</button>
        </form>
        <script>
            const chat = document.getElementById("chat");
            const form = document.getElementById("form");
            const input = document.getElementById("query");

            function markdown(text) {
                if (window.marked) {
                    return marked.parse(text);
                }
                return `<pre>${text}</pre>`;
            }

            function append(role, html) {
                const section = document.createElement("section");
                section.className = `message ${role}`;
                section.innerHTML = `<div class="content">${html}</div>`;
                chat.appendChild(section);
                chat.scrollTop = chat.scrollHeight;
                return section.querySelector(".content");
            }

            form.addEventListener("submit", async (event) => {
                event.preventDefault();
                const query = input.value.trim();
                if (!query) return;

                append("user", `<p>${query}</p>`);
                input.value = "";

                const content = append(
                    "assistant",
                    `<p class="status">Starting agent...</p><div class="answer"></div><div class="source-list"></div>`
                );
                const status = content.querySelector(".status");
                const answer = content.querySelector(".answer");
                const sourceList = content.querySelector(".source-list");
                let fullText = "";

                try {
                    const response = await fetch(`/api/chat_stream?query=${encodeURIComponent(query)}&no_cache=true`);
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder("utf-8");
                    let buffer = "";

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        let boundary = buffer.indexOf("\\n\\n");

                        while (boundary !== -1) {
                            const eventText = buffer.slice(0, boundary);
                            buffer = buffer.slice(boundary + 2);

                            for (const line of eventText.split("\\n")) {
                                if (!line.startsWith("data: ")) continue;

                                const data = JSON.parse(line.slice(6));
                                if (data.type === "status") {
                                    status.textContent = data.content;
                                } else if (data.type === "chunk") {
                                    status.style.display = "none";
                                    fullText += data.content;
                                    answer.innerHTML = markdown(fullText);
                                } else if (data.type === "done") {
                                    status.style.display = "none";
                                    if (data.result.success === false) {
                                        answer.textContent = `Agent Error: ${data.result.error || "No results found"}`;
                                        return;
                                    }
                                    if (data.result.analysis) {
                                        answer.innerHTML = markdown(data.result.analysis);
                                    }
                                    if (data.result.results?.length) {
                                        sourceList.innerHTML = `<div class="sources"><strong>Sources Read</strong>${
                                            data.result.results.slice(0, 10).map((source) => `
                                                <div class="source">
                                                    <a href="${source.url}" target="_blank" rel="noreferrer">${source.title}</a>
                                                    <div class="url">${source.url}</div>
                                                    ${(source.reference_urls || []).length ? `
                                                        <div class="url">Extraction refs: ${
                                                            source.reference_urls.map((url) => `<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`).join(" | ")
                                                        }</div>
                                                    ` : ""}
                                                </div>
                                            `).join("")
                                        }${
                                            (data.result.reference_urls || []).length ? `
                                                <div class="source">
                                                    <strong>Extraction Reference URLs</strong>
                                                    ${(data.result.reference_urls || []).slice(0, 20).map((url) => `
                                                        <div class="url"><a href="${url}" target="_blank" rel="noreferrer">${url}</a></div>
                                                    `).join("")}
                                                </div>
                                            ` : ""
                                        }</div>`;
                                    }
                                } else if (data.type === "error") {
                                    status.textContent = `Error: ${data.content}`;
                                }
                            }

                            boundary = buffer.indexOf("\\n\\n");
                        }
                    }
                } catch (error) {
                    status.textContent = `Network Error: ${error.message}`;
                }
            });
        </script>
    </body>
    </html>
    """
