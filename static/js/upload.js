(() => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const picker = document.getElementById("file-picker");
  const zone = document.getElementById("drop-zone");
  const selection = document.getElementById("selection");
  const button = document.getElementById("upload-button");
  const progress = document.getElementById("progress-wrap");
  const progressBar = document.getElementById("progress-bar");
  const progressLabel = document.getElementById("progress-label");
  let chosenFiles = [];

  function renderSelection(files) {
    chosenFiles = Array.from(files);
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
  function formatSize(bytes) {
    if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
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
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!chosenFiles.length) return;
    button.disabled = true;
    progress.hidden = false;
    progressLabel.textContent = "Загрузка файлов…";
    const data = new FormData(form);
    data.delete("files");
    chosenFiles.forEach((file) => data.append("files", file, file.name));
    const request = new XMLHttpRequest();
    request.open("POST", form.action);
    request.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    request.upload.addEventListener("progress", (progressEvent) => {
      if (!progressEvent.lengthComputable) return;
      const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
      progressBar.style.width = `${percent}%`;
      progressLabel.textContent = `Передача файлов: ${percent}%`;
    });
    request.addEventListener("load", () => {
      try {
        const response = JSON.parse(request.responseText);
        progressLabel.textContent = response.results
          ? response.results.map((result) => `${result.name}: ${result.message}`).join(" · ")
          : (response.errors || ["Ошибка загрузки."]).join(" · ");
      } catch {
        progressLabel.textContent = "Не удалось обработать ответ сервера.";
      }
      window.setTimeout(() => window.location.reload(), 1400);
    });
    request.addEventListener("error", () => {
      progressLabel.textContent = "Ошибка сети. Проверьте подключение и обновите страницу.";
      button.disabled = false;
    });
    request.addEventListener("abort", () => {
      progressLabel.textContent = "Загрузка отменена.";
      button.disabled = false;
    });
    request.send(data);
  });
})();
