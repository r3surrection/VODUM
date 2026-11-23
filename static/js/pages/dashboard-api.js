// Fork addition: admin page "Dashboard API" (templates/dashboard_api/settings.html).
(() => {
  "use strict";
  const root = document.getElementById("dashboard-api-page");
  if (!root) return;
  const copiedLabel = root.getAttribute("data-copied-label") || "Copied";

  root.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const node = document.getElementById(button.getAttribute("data-copy-target"));
      if (!node || !navigator.clipboard) return;
      navigator.clipboard.writeText(node.textContent.trim()).then(() => {
        const label = button.textContent;
        button.textContent = copiedLabel;
        setTimeout(() => { button.textContent = label; }, 2000);
      });
    });
  });

  root.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });
})();
