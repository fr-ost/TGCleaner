// Clicking the toolbar icon opens the dashboard in a tab (or focuses it if already open).
// runtime.getContexts finds our own tabs without needing the "tabs" permission.
const DASHBOARD = chrome.runtime.getURL("dashboard.html");

chrome.action.onClicked.addListener(async () => {
  const [existing] = await chrome.runtime.getContexts({ contextTypes: ["TAB"], documentUrls: [DASHBOARD] });
  if (existing && existing.tabId >= 0) {
    await chrome.tabs.update(existing.tabId, { active: true });
    await chrome.windows.update(existing.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url: DASHBOARD });
  }
});
