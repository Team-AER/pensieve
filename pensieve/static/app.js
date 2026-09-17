// Global bootstrap only: service-worker registration (versioned by the build stamp base.html passes in).
// Theme bootstrap is the inline script in base.html; feature scripts live in reader.js.
(function () {
  var me = document.currentScript;
  var sw = me && me.getAttribute('data-sw');
  if (sw && 'serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register(sw, { scope: '/' }).catch(function () {});
    });
  }
})();
