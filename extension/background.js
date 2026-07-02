const ALARM_NAME = 'export_reading_list';
const TARGET_FILE = 'HugoSync/reading_list.json';
const PERIOD_MINUTES = 30;

async function exportReadingList() {
  try {
    const entries = await chrome.readingList.query({});
    const payload = {
      exportedAt: new Date().toISOString(),
      count: entries.length,
      entries: entries.map((e) => ({
        url: e.url,
        title: e.title,
        hasBeenRead: e.hasBeenRead,
        creationTime: e.creationTime,
        lastUpdateTime: e.lastUpdateTime,
      })),
    };
    const json = JSON.stringify(payload, null, 2);
    const dataUrl =
      'data:application/json;charset=utf-8,' + encodeURIComponent(json);
    await chrome.downloads.download({
      url: dataUrl,
      filename: TARGET_FILE,
      conflictAction: 'overwrite',
      saveAs: false,
    });
    console.log(`[reading-list-exporter] exported ${entries.length} entries`);
  } catch (err) {
    console.error('[reading-list-exporter] export failed', err);
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: PERIOD_MINUTES });
  exportReadingList();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: PERIOD_MINUTES });
  exportReadingList();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    exportReadingList();
  }
});

if (chrome.readingList.onEntryAdded) {
  chrome.readingList.onEntryAdded.addListener(exportReadingList);
}
if (chrome.readingList.onEntryUpdated) {
  chrome.readingList.onEntryUpdated.addListener(exportReadingList);
}
if (chrome.readingList.onEntryRemoved) {
  chrome.readingList.onEntryRemoved.addListener(exportReadingList);
}
