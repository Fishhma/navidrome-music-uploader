(() => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const picker = document.getElementById("file-picker");
  const zone = document.getElementById("drop-zone");
  const selection = document.getElementById("selection");
  const passwordWrap = document.getElementById("archive-password-wrap");
  const archivePassword = document.getElementById("archive-password");
  const button = document.getElementById("upload-button");
  const progress = document.getElementById("progress-wrap");
  const progressBar = document.getElementById("progress-bar");
  const progressLabel = document.getElementById("progress-label");
  const resultsBox = document.getElementById("results");
  const historyBox = document.getElementById("history");
  const quotaSummary = document.getElementById("quota-summary");
  const stringsNode = document.getElementById("upload-strings");
  const t = stringsNode ? JSON.parse(stringsNode.textContent) : {};
  const text = (key, values = {}) => (t[key] || key).replace(/%\((\w+)\)s/g, (_, name) => values[name] ?? "");
  let chosenFiles = [];
  let busy = false;

  function isArchive(file) {
    return /\.(rar|zip)$/i.test(file.name);
  }

  function formatSize(bytes) {
    if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  function renderSelection(files) {
    if (busy) return;
    chosenFiles = Array.from(files);
    const hasArchive = chosenFiles.some(isArchive);
    passwordWrap.hidden = !hasArchive;
    if (!hasArchive) archivePassword.value = "";
    selection.replaceChildren();
    chosenFiles.forEach((file) => {
      const chip = document.createElement("span");
      chip.className = "file-chip";
      chip.textContent = `${file.name} · ${formatSize(file.size)}`;
      selection.append(chip);
    });
    selection.hidden = chosenFiles.length === 0;
    button.disabled = chosenFiles.length === 0;
  }

  function renderResults(results, scanStarted) {
    resultsBox.replaceChildren();
    results.forEach((result) => {
      const row = document.createElement("div");
      row.className = `result-row ${result.status}`;
      const head = document.createElement("div");
      head.className = "result-head";
      const badge = document.createElement("span");
      badge.className = `status ${result.status}`;
      badge.textContent = (t.status && t.status[result.status]) || result.status;
      const name = document.createElement("strong");
      name.textContent = result.name;
      head.append(badge, name);
      const message = document.createElement("p");
      message.className = "muted";
      message.textContent = result.message;
      row.append(head, message);
      if (result.details && result.details.length) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = `${text("details")} (${result.details.length})`;
        const list = document.createElement("ul");
        result.details.forEach((detail) => {
          const item = document.createElement("li");
          item.className = detail.status;
          item.textContent = `${detail.name.split("/").pop()} — ${detail.message}`;
          list.append(item);
        });
        details.append(summary, list);
        row.append(details);
      }
      resultsBox.append(row);
    });
    if (scanStarted) {
      const note = document.createElement("p");
      note.className = "muted scan-note";
      note.textContent = text("scanStarted");
      resultsBox.append(note);
    }
    resultsBox.hidden = false;
  }

  function showError(message) {
    resultsBox.replaceChildren();
    const row = document.createElement("div");
    row.className = "result-row failed";
    row.textContent = message;
    resultsBox.append(row);
    resultsBox.hidden = false;
  }

  async function refreshHistory() {
    try {
      const response = await fetch(form.dataset.historyUrl, { headers: { "X-Requested-With": "XMLHttpRequest" }, credentials: "same-origin" });
      if (!response.ok) return;
      const html = await response.text();
      const holder = document.createElement("div");
      holder.innerHTML = html;
      const quota = holder.querySelector("#quota-card");
      if (quota && quotaSummary) {
        quotaSummary.innerHTML = quota.innerHTML;
        quota.remove();
      }
      historyBox.replaceChildren(...holder.childNodes);
    } catch {
      /* History refresh is cosmetic. */
    }
  }

  function finish() {
    busy = false;
    chosenFiles = [];
    picker.value = "";
    selection.replaceChildren();
    selection.hidden = true;
    button.disabled = true;
    refreshHistory();
  }

  zone.addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => renderSelection(picker.files));
  ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
    event.preventDefault();
    zone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
  }));
  zone.addEventListener("drop", (event) => renderSelection(event.dataTransfer.files));
  window.addEventListener("beforeunload", (event) => {
    if (busy) event.preventDefault();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!chosenFiles.length || busy) return;
    busy = true;
    button.disabled = true;
    resultsBox.hidden = true;
    progress.hidden = false;
    progress.classList.remove("processing");
    progressBar.style.width = "0%";
    progressLabel.textContent = text("uploading");
    const data = new FormData(form);
    data.delete("files");
    data.set("archive_password", archivePassword.value);
    chosenFiles.forEach((file) => data.append("files", file, file.name));
    const request = new XMLHttpRequest();
    request.open("POST", form.action);
    request.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    request.upload.addEventListener("progress", (progressEvent) => {
      if (!progressEvent.lengthComputable) return;
      const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
      progressBar.style.width = `${percent}%`;
      progressLabel.textContent = text("transferring", { percent: `${percent}%` });
    });
    request.upload.addEventListener("load", () => {
      progressBar.style.width = "100%";
      progress.classList.add("processing");
      progressLabel.textContent = text("processing");
    });
    request.addEventListener("load", () => {
      progress.classList.remove("processing");
      let response = null;
      try {
        response = JSON.parse(request.responseText);
      } catch {
        response = null;
      }
      if (response && response.results) {
        progressLabel.textContent = text("done");
        renderResults(response.results, response.scan_started);
        archivePassword.value = "";
      } else if (response && response.errors) {
        progressLabel.textContent = text("uploadError");
        showError(response.errors.join(" · "));
      } else if (request.status === 413) {
        progressLabel.textContent = text("uploadError");
        showError(text("tooLarge"));
      } else if (request.status === 403 || request.responseURL.includes("/accounts/login/")) {
        progressLabel.textContent = text("uploadError");
        showError(text("sessionExpired"));
      } else {
        progressLabel.textContent = text("uploadError");
        showError(request.status >= 400 ? text("serverError", { status: request.status }) : text("parseError"));
      }
      finish();
    });
    request.addEventListener("error", () => {
      progress.classList.remove("processing");
      progressLabel.textContent = text("networkError");
      busy = false;
      button.disabled = false;
    });
    request.addEventListener("abort", () => {
      progress.classList.remove("processing");
      progressLabel.textContent = text("cancelled");
      busy = false;
      button.disabled = false;
    });
    request.send(data);
  });
})();
