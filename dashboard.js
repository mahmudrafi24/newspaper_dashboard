const status = document.querySelector("[data-revision]");
const notice = document.querySelector("#refresh-notice");

async function checkResearch() {
  try {
    const response = await fetch("/admin/revision");
    if (response.ok && (await response.text()) !== status.dataset.revision) {
      notice.hidden = false;
      return;
    }
  } catch {
    // Allow brief network interruptions and check again.
  }
  window.setTimeout(checkResearch, 30000);
}

window.setTimeout(checkResearch, 30000);

for (const card of document.querySelectorAll("[data-editor-story]")) {
  const fields = [
    [card.querySelector("input[name=headline]"), card.querySelector("[data-preview-headline]")],
    [card.querySelector("textarea[name=summary]"), card.querySelector("[data-preview-summary]")],
  ];
  for (const [input, preview] of fields) {
    if (input) {
      input.addEventListener("input", () => {
        preview.textContent = input.value;
      });
    }
  }
}
