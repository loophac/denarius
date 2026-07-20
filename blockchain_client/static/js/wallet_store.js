(function (global) {
  "use strict";

  var LEGACY_STORAGE_KEY = "denarius.encryptedWallets.v1";
  var scopeMeta = document.querySelector('meta[name="denarius-wallet-scope"]');
  var migrationMeta = document.querySelector('meta[name="denarius-wallet-migrate-legacy"]');
  var walletScope = scopeMeta ? scopeMeta.getAttribute("content") : "unscoped";
  var STORAGE_KEY = "denarius.encryptedWallets.v2." + encodeURIComponent(walletScope);

  if (
    migrationMeta && migrationMeta.getAttribute("content") === "true" &&
    !global.localStorage.getItem(STORAGE_KEY) &&
    global.localStorage.getItem(LEGACY_STORAGE_KEY)
  ) {
    global.localStorage.setItem(STORAGE_KEY, global.localStorage.getItem(LEGACY_STORAGE_KEY));
    global.localStorage.removeItem(LEGACY_STORAGE_KEY);
  }

  function list() {
    try {
      var wallets = JSON.parse(global.localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(wallets) ? wallets.filter(function (wallet) {
        return wallet && typeof wallet === "object" && typeof wallet.address === "string";
      }) : [];
    } catch (error) {
      return [];
    }
  }

  function save(wallet) {
    if (!wallet || typeof wallet !== "object" || typeof wallet.address !== "string") {
      throw new Error("Invalid encrypted wallet");
    }
    var wallets = list().filter(function (existing) {
      return existing.address !== wallet.address;
    });
    wallets.push(wallet);
    global.localStorage.setItem(STORAGE_KEY, JSON.stringify(wallets));
    return wallet;
  }

  function get(address) {
    return list().find(function (wallet) {
      return wallet.address === address;
    }) || null;
  }

  function remove(address) {
    var wallets = list().filter(function (wallet) {
      return wallet.address !== address;
    });
    global.localStorage.setItem(STORAGE_KEY, JSON.stringify(wallets));
  }

  function exportWallet(wallet, filename) {
    var blob = new Blob([JSON.stringify(wallet, null, 2)], {type: "application/json"});
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename || "denarius-wallet.denwallet";
    document.body.appendChild(link);
    link.click();
    link.remove();
    global.setTimeout(function () {
      URL.revokeObjectURL(link.href);
    }, 0);
  }

  global.DenariusWalletStore = {
    list: list,
    save: save,
    get: get,
    remove: remove,
    exportWallet: exportWallet
  };
})(window);
