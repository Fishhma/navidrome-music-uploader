(() => {
  const form = document.getElementById("upload-form");
  if (!form) return;
  const picker = document.getElementById("file-picker");
  const folderPicker = document.getElementById("folder-picker");
  const folderButton = document.getElementById("folder-button");
  const zone = document.getElementById("drop-zone");
  const selection = document.getElementById("selection");
  const button = document.getElementById("upload-button");
  const progress = document.getElementById("progress-wrap");
  const progressBar = document.getElementById("progress-bar");
  const progressLabel = document.getElementById("progress-label");
  const passwordDialog = document.getElementById("archive-password-dialog");
  const passwordInput = document.getElementById("archive-password-input");
  const passwordMessage = document.getElementById("archive-password-message");
  const passwordSubmit = document.getElementById("archive-password-submit");
  const passwordCancel = document.getElementById("archive-password-cancel");
  let chosenFiles = [];
  let folderMode = false;

  function renderSelection(files, fromFolder = false) {
    chosenFiles = Array.from(files);
    folderMode = fromFolder;
    selection.replaceChildren();
    chosenFiles.forEach((file) => {
      const chip = document.createElement("span");
      chip.className = "file-chip";
      const label = fromFolder && file.webkitRelativePath ? file.webkitRelativePath : file.name;
      chip.textContent = `${label} · ${formatSize(file.size)}`;
      selection.append(chip);
    });
    selection.hidden = chosenFiles.length === 0;
    button.disabled = chosenFiles.length === 0;
  }

  function formatSize(bytes) {
    if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  zone.addEventListener("click", () => picker.click());
  folderButton.addEventListener("click", () => folderPicker.click());
  picker.addEventListener("change", () => {
    folderPicker.value = "";
    renderSelection(picker.files, false);
  });
  folderPicker.addEventListener("change", () => {
    picker.value = "";
    renderSelection(folderPicker.files, true);
  });
  ["dragenter", "dragover"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
    event.preventDefault();
    zone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((eventName) => zone.addEventListener(eventName, (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
  }));
  zone.addEventListener("drop", (event) => {
    picker.value = "";
    folderPicker.value = "";
    renderSelection(event.dataTransfer.files, false);
  });


  function askPassword(message) {
    return new Promise((resolve) => {
      passwordMessage.textContent = message;
      passwordInput.value = "";
      const finish = (value) => {
        passwordDialog.close();
        passwordSubmit.removeEventListener("click", onSubmit);
        passwordCancel.removeEventListener("click", onCancel);
        passwordDialog.removeEventListener("cancel", onDialogCancel);
        passwordInput.removeEventListener("keydown", onKeyDown);
        resolve(value);
      };
      const onSubmit = () => finish(passwordInput.value);
      const onCancel = () => finish(null);
      const onDialogCancel = (event) => { event.preventDefault(); finish(null); };
      const onKeyDown = (event) => {
        if (event.key === "Enter") { event.preventDefault(); finish(passwordInput.value); }
      };
      passwordSubmit.addEventListener("click", onSubmit);
      passwordCancel.addEventListener("click", onCancel);
      passwordDialog.addEventListener("cancel", onDialogCancel);
      passwordInput.addEventListener("keydown", onKeyDown);
      passwordDialog.showModal();
      passwordInput.focus();
    });
  }

  function sendFiles(files, options = {}) {
    const { passwordByIndex = {}, paths = null, isFolder = false, showProgress = false } = options;
    return new Promise((resolve, reject) => {
      const data = new FormData(form);
      data.delete("files");
      data.delete("relative_paths");
      data.delete("folder_upload");
      for (const key of Array.from(data.keys())) {
        if (key.startsWith("archive_password_")) data.delete(key);
      }
      files.forEach((file, index) => {
        data.append("files", file, file.name);
        if (Object.prototype.hasOwnProperty.call(passwordByIndex, index)) {
          data.append(`archive_password_${index}`, passwordByIndex[index]);
        }
      });
      if (isFolder && paths) {
        data.append("folder_upload", "1");
        data.append("relative_paths", JSON.stringify(paths));
      }
      const request = new XMLHttpRequest();
      request.open("POST", form.action);
      request.setRequestHeader("X-Requested-With", "XMLHttpRequest");
      request.upload.addEventListener("progress", (event) => {
        if (!showProgress || !event.lengthComputable) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        progressBar.style.width = `${percent}%`;
        progressLabel.textContent = `${form.dataset.transfer} ${percent}%`;
      });
      request.addEventListener("load", () => {
        try {
          const response = JSON.parse(request.responseText);
          if (request.status >= 400 && !response.results) resolve({ errors: response.errors || [form.dataset.uploadError] });
          else resolve(response);
        } catch {
          resolve({ errors: [form.dataset.responseError] });
        }
      });
      request.addEventListener("error", () => reject(new Error(form.dataset.networkError)));
      request.addEventListener("abort", () => reject(new Error(form.dataset.aborted)));
      request.send(data);
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!chosenFiles.length) return;
    button.disabled = true;
    folderButton.disabled = true;
    picker.disabled = true;
    folderPicker.disabled = true;
    progress.hidden = false;
    progressLabel.textContent = form.dataset.uploading;
    const paths = folderMode ? chosenFiles.map((file) => file.webkitRelativePath || file.name) : null;
    try {
      const response = await sendFiles(chosenFiles, { paths, isFolder: folderMode, showProgress: true });
      if (!response.results) {
        progressLabel.textContent = (response.errors || [form.dataset.uploadError]).join(" · ");
        window.setTimeout(() => window.location.reload(), 1800);
        return;
      }
      for (let i = 0; i < response.results.length; i += 1) {
        let item = response.results[i];
        if (!item.needs_password) continue;
        let attempts = 0;
        let finished = false;
        while (attempts < 3 && !finished) {
          const prefix = item.password_invalid ? `${form.dataset.passwordInvalid}\n` : "";
          const password = await askPassword(`${prefix}${form.dataset.passwordPrompt}\n${item.name}`);
          if (password === null) {
            response.results[i] = { ...item, status: "failed", needs_password: false, message: form.dataset.passwordCancelled };
            finished = true;
            break;
          }
          attempts += 1;
          progressLabel.textContent = `${form.dataset.uploading} ${item.name}`;
          const retry = await sendFiles([chosenFiles[item.index]], { passwordByIndex: { 0: password } });
          const retried = retry.results && retry.results[0];
          if (retried && retried.needs_password) {
            item = { ...item, ...retried, index: response.results[i].index };
            response.results[i] = item;
            continue;
          }
          response.results[i] = retried
            ? { ...retried, index: response.results[i].index }
            : { ...item, status: "failed", needs_password: false, message: (retry.errors || [form.dataset.uploadError]).join(" · ") };
          finished = true;
        }
        if (!finished && item.needs_password) {
          response.results[i] = { ...item, status: "failed", needs_password: false, message: form.dataset.passwordAttempts };
        }
      }
      progressLabel.textContent = response.results.map((result) => `${result.name}: ${result.message}`).join(" · ");
      window.setTimeout(() => window.location.reload(), 2200);
    } catch (error) {
      progressLabel.textContent = error.message || form.dataset.networkError;
      button.disabled = false;
      folderButton.disabled = false;
      picker.disabled = false;
      folderPicker.disabled = false;
    }
  });
})();
