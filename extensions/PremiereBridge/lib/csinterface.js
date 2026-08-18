/**
 * Minimal CSInterface shim.
 *
 * Adobe ships a ~1000-line CSInterface.js covering the whole CEP surface
 * (themes, events, extension management, host environment structs). This
 * bridge needs three things from it: evalScript, the extension's own path, and
 * the host application id. Wrapping just those keeps a vendored blob out of
 * the repo and makes the CEP dependency auditable at a glance.
 *
 * window.__adobe_cep__ is the native object CEP injects into every panel; the
 * official CSInterface.js is itself a wrapper over exactly these calls.
 */
(function (global) {
  'use strict';

  function CSInterface() {}

  CSInterface.prototype.evalScript = function (script, callback) {
    if (!global.__adobe_cep__) {
      if (callback) { callback('EvalScript error: CEP runtime unavailable'); }
      return;
    }
    global.__adobe_cep__.evalScript(script, callback || function () {});
  };

  CSInterface.prototype.getSystemPath = function (type) {
    if (!global.__adobe_cep__) { return ''; }
    var path = decodeURI(global.__adobe_cep__.getSystemPath(type));
    // CEP hands back a file:// URL; Node's fs wants a plain path.
    path = path.replace(/^file\:\/{2,3}/, '');
    if (/^\/[A-Za-z]:/.test(path)) { path = path.substring(1); }
    return path;
  };

  CSInterface.prototype.getHostEnvironment = function () {
    if (!global.__adobe_cep__) { return {}; }
    try {
      return JSON.parse(global.__adobe_cep__.getHostEnvironment());
    } catch (e) {
      return {};
    }
  };

  CSInterface.prototype.getExtensionID = function () {
    return global.__adobe_cep__ ? global.__adobe_cep__.getExtensionId() : '';
  };

  var SystemPath = {
    EXTENSION: 'extension',
    USER_DATA: 'userData',
    COMMON_FILES: 'commonFiles',
    MY_DOCUMENTS: 'myDocuments',
    HOST_APPLICATION: 'hostApplication'
  };

  global.CSInterface = CSInterface;
  global.SystemPath = SystemPath;
}(typeof window !== 'undefined' ? window : this));
