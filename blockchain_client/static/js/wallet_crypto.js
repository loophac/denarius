(function (global) {
  "use strict";

  var WALLET_FORMAT = "denarius-wallet";
  var WALLET_VERSION = 2;
  var KDF_ITERATIONS = 600000;
  var ATOMIC_UNITS = 100000000n;
  var MIN_PASSWORD_LENGTH = 10;
  var WALLET_FIELDS = [
    "format",
    "version",
    "address",
    "public_key",
    "private_key_format",
    "cipher",
    "kdf",
    "kdf_iterations",
    "salt",
    "nonce",
    "ciphertext"
  ];

  function requireWebCrypto() {
    if (!global.crypto || !global.crypto.subtle) {
      throw new Error("This browser does not provide the secure cryptography required by Denarius.");
    }
    return global.crypto.subtle;
  }

  function bytesToHex(value) {
    return Array.from(new Uint8Array(value), function (byte) {
      return byte.toString(16).padStart(2, "0");
    }).join("");
  }

  function hexToBytes(value) {
    if (typeof value !== "string" || value.length % 2 || !/^[0-9a-f]+$/.test(value)) {
      throw new Error("Invalid hexadecimal wallet data.");
    }
    var bytes = new Uint8Array(value.length / 2);
    for (var index = 0; index < value.length; index += 2) {
      bytes[index / 2] = parseInt(value.slice(index, index + 2), 16);
    }
    return bytes;
  }

  function utf8(value) {
    return new TextEncoder().encode(value);
  }

  function canonicalJson(value) {
    if (Array.isArray(value)) {
      return "[" + value.map(canonicalJson).join(",") + "]";
    }
    if (value && typeof value === "object") {
      return "{" + Object.keys(value).sort().map(function (key) {
        return JSON.stringify(key) + ":" + canonicalJson(value[key]);
      }).join(",") + "}";
    }
    return JSON.stringify(value);
  }

  async function sha256Hex(value) {
    return bytesToHex(await requireWebCrypto().digest("SHA-256", value));
  }

  async function addressFromPublicKey(publicKeyBytes) {
    var publicKeyHex = bytesToHex(publicKeyBytes);
    var checksum = (await sha256Hex(utf8("DENARIUS:" + publicKeyHex))).slice(0, 8);
    return "dn" + publicKeyHex + checksum;
  }

  function validatePassword(password) {
    if (typeof password !== "string" || password.length < MIN_PASSWORD_LENGTH) {
      throw new Error("Wallet password must be at least 10 characters.");
    }
  }

  async function deriveEncryptionKey(password, salt, usage) {
    validatePassword(password);
    var subtle = requireWebCrypto();
    var passwordKey = await subtle.importKey("raw", utf8(password), "PBKDF2", false, ["deriveKey"]);
    return subtle.deriveKey(
      {name: "PBKDF2", hash: "SHA-256", salt: salt, iterations: KDF_ITERATIONS},
      passwordKey,
      {name: "AES-GCM", length: 256},
      false,
      [usage]
    );
  }

  function walletMetadata(wallet) {
    var metadata = {};
    WALLET_FIELDS.slice(0, -1).forEach(function (field) {
      metadata[field] = wallet[field];
    });
    return metadata;
  }

  async function inspect(wallet) {
    if (!wallet || typeof wallet !== "object" || Array.isArray(wallet)) {
      throw new Error("Invalid Denarius wallet backup.");
    }
    var keys = Object.keys(wallet).sort();
    if (JSON.stringify(keys) !== JSON.stringify(WALLET_FIELDS.slice().sort())) {
      throw new Error("Invalid Denarius wallet backup.");
    }
    if (
      wallet.format !== WALLET_FORMAT || wallet.version !== WALLET_VERSION ||
      wallet.private_key_format !== "pkcs8-ed25519" || wallet.cipher !== "aes-256-gcm" ||
      wallet.kdf !== "pbkdf2-sha256" || wallet.kdf_iterations !== KDF_ITERATIONS
    ) {
      throw new Error("Unsupported Denarius wallet format.");
    }
    var publicKey = hexToBytes(wallet.public_key);
    if (publicKey.length !== 32 || hexToBytes(wallet.salt).length !== 16 ||
        hexToBytes(wallet.nonce).length !== 12 || hexToBytes(wallet.ciphertext).length < 32) {
      throw new Error("Invalid Denarius wallet backup.");
    }
    var expectedAddress = await addressFromPublicKey(publicKey);
    if (wallet.address !== expectedAddress) {
      throw new Error("Wallet address does not match its public key.");
    }
    return {address: wallet.address, public_key: wallet.public_key};
  }

  async function create(password) {
    validatePassword(password);
    var subtle = requireWebCrypto();
    var pair = await subtle.generateKey({name: "Ed25519"}, true, ["sign", "verify"]);
    var publicKey = new Uint8Array(await subtle.exportKey("raw", pair.publicKey));
    var privateKey = new Uint8Array(await subtle.exportKey("pkcs8", pair.privateKey));
    var salt = global.crypto.getRandomValues(new Uint8Array(16));
    var nonce = global.crypto.getRandomValues(new Uint8Array(12));
    var wallet = {
      format: WALLET_FORMAT,
      version: WALLET_VERSION,
      address: await addressFromPublicKey(publicKey),
      public_key: bytesToHex(publicKey),
      private_key_format: "pkcs8-ed25519",
      cipher: "aes-256-gcm",
      kdf: "pbkdf2-sha256",
      kdf_iterations: KDF_ITERATIONS,
      salt: bytesToHex(salt),
      nonce: bytesToHex(nonce)
    };
    var encryptionKey = await deriveEncryptionKey(password, salt, "encrypt");
    try {
      wallet.ciphertext = bytesToHex(await subtle.encrypt(
        {name: "AES-GCM", iv: nonce, additionalData: utf8(canonicalJson(walletMetadata(wallet)))},
        encryptionKey,
        privateKey
      ));
    } finally {
      privateKey.fill(0);
    }
    return wallet;
  }

  async function decryptPrivateKey(wallet, password) {
    await inspect(wallet);
    var subtle = requireWebCrypto();
    var salt = hexToBytes(wallet.salt);
    var nonce = hexToBytes(wallet.nonce);
    var key = await deriveEncryptionKey(password, salt, "decrypt");
    try {
      return new Uint8Array(await subtle.decrypt(
        {name: "AES-GCM", iv: nonce, additionalData: utf8(canonicalJson(walletMetadata(wallet)))},
        key,
        hexToBytes(wallet.ciphertext)
      ));
    } catch (error) {
      throw new Error("Invalid wallet password or damaged wallet backup.");
    }
  }

  function parseDenarii(value) {
    var text = String(value).trim();
    if (!/^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$/.test(text)) {
      throw new Error("DEN amounts must be positive with no more than 8 decimal places.");
    }
    var parts = text.split(".");
    var atomic = BigInt(parts[0]) * ATOMIC_UNITS;
    atomic += BigInt((parts[1] || "").padEnd(8, "0") || "0");
    if (atomic <= 0n) {
      throw new Error("DEN amounts must be greater than zero.");
    }
    return atomic.toString();
  }

  async function signTransaction(wallet, password, details, protocol) {
    if (!protocol || !Number.isInteger(protocol.protocol_version) || typeof protocol.network !== "string") {
      throw new Error("The node did not provide valid protocol information.");
    }
    var nonce = Number(details.nonce);
    if (!Number.isSafeInteger(nonce) || nonce < 0) {
      throw new Error("Invalid account nonce.");
    }
    var payload = {
      version: protocol.protocol_version,
      network: protocol.network,
      sender_address: wallet.address,
      recipient_address: String(details.recipient_address),
      amount_atomic: parseDenarii(details.amount),
      fee_atomic: parseDenarii(details.fee),
      nonce: nonce
    };
    var privateBytes = await decryptPrivateKey(wallet, password);
    var subtle = requireWebCrypto();
    try {
      var privateKey = await subtle.importKey(
        "pkcs8",
        privateBytes,
        {name: "Ed25519"},
        false,
        ["sign"]
      );
      var message = utf8(canonicalJson(payload));
      var signatureBytes = await subtle.sign({name: "Ed25519"}, privateKey, message);
      var publicKey = await subtle.importKey(
        "raw",
        hexToBytes(wallet.public_key),
        {name: "Ed25519"},
        false,
        ["verify"]
      );
      if (!await subtle.verify({name: "Ed25519"}, publicKey, signatureBytes, message)) {
        throw new Error("Wallet private key does not match its public key.");
      }
      var signature = bytesToHex(signatureBytes);
      var signedPayload = Object.assign({}, payload, {signature: signature});
      return {
        transaction: payload,
        signature: signature,
        transaction_id: await sha256Hex(utf8(canonicalJson(signedPayload)))
      };
    } finally {
      privateBytes.fill(0);
    }
  }

  function importBackup(text) {
    var wallet;
    try {
      wallet = JSON.parse(String(text));
    } catch (error) {
      throw new Error("Wallet backup is not valid JSON.");
    }
    return inspect(wallet).then(function () { return wallet; });
  }

  global.DenariusWalletCrypto = {
    create: create,
    inspect: inspect,
    importBackup: importBackup,
    parseDenarii: parseDenarii,
    signTransaction: signTransaction,
    canonicalJson: canonicalJson
  };
})(typeof window === "undefined" ? globalThis : window);
