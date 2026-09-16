const bridge = window.AstrBotPluginPage;
const list = document.getElementById("holiday-list");
const emptyState = document.getElementById("empty-state");
const template = document.getElementById("holiday-row");
const notice = document.getElementById("notice");

function updateEmptyState() {
  emptyState.hidden = list.children.length !== 0;
}

function setNotice(message, isError = false) {
  notice.textContent = message;
  notice.dataset.state = isError ? "error" : "success";
}

function addRow(period = {}) {
  const row = template.content.firstElementChild.cloneNode(true);
  row.querySelector(".holiday-name").value = period.name || "";
  row.querySelector(".holiday-start").value = period.start || "";
  row.querySelector(".holiday-end").value = period.end || "";
  row.querySelector(".delete").addEventListener("click", () => {
    row.remove();
    updateEmptyState();
  });
  list.append(row);
  updateEmptyState();
}

function readPeriods() {
  return [...list.querySelectorAll("tr")].map((row) => ({
    name: row.querySelector(".holiday-name").value.trim(),
    start: row.querySelector(".holiday-start").value,
    end: row.querySelector(".holiday-end").value,
  }));
}

async function loadPeriods() {
  setNotice("正在加载...");
  const data = await bridge.apiGet("holidays");
  list.replaceChildren();
  (data.periods || []).forEach(addRow);
  updateEmptyState();
  setNotice("");
}

document.getElementById("add-holiday").addEventListener("click", () => addRow());
document.getElementById("reload").addEventListener("click", () => {
  loadPeriods().catch((error) => setNotice(error.message || "加载失败", true));
});

document.getElementById("holiday-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const periods = readPeriods();
  if (periods.some((period) => period.end < period.start)) {
    setNotice("结束日期不能早于开始日期。", true);
    return;
  }
  const button = document.getElementById("save");
  button.disabled = true;
  setNotice("正在保存...");
  try {
    const data = await bridge.apiPost("holidays/save", { periods });
    list.replaceChildren();
    (data.periods || []).forEach(addRow);
    updateEmptyState();
    setNotice("已保存。");
  } catch (error) {
    setNotice(error.message || "保存失败", true);
  } finally {
    button.disabled = false;
  }
});

await bridge.ready();
loadPeriods().catch((error) => setNotice(error.message || "加载失败", true));
