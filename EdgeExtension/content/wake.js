// Wakes the MV3 service worker on every page load.
//
// A terminated service worker cannot be woken by its own timers, but a
// chrome.runtime.sendMessage() call from a content script always starts it.
// Combined with the background keepalive alarm (which only exists once the
// worker has run at least once), this makes the bridge self-healing even if
// Nova's server restarts while the worker is asleep: the next page load
// (or alarm tick) brings it back and it re-joins the bridge.
try {
  chrome.runtime.sendMessage({ type: 'nova-wake' }, () => {
    // Swallow the "Could not establish connection" error that fires when the
    // worker is mid-restart -- the message itself still woke it.
    void chrome.runtime.lastError;
  });
} catch (_) {
  // context invalidated mid-send; nothing to do, the next page load retries
}
