import assert from "node:assert/strict";

await import("../blockchain_client/static/js/wallet_crypto.js");

const password = "correct horse battery staple";
const wallet = await globalThis.DenariusWalletCrypto.create(password);
assert.equal(wallet.version, 2);
assert.equal(wallet.address.length, 74);
assert.equal("private_key" in wallet, false);
assert.deepEqual(
  await globalThis.DenariusWalletCrypto.inspect(wallet),
  {address: wallet.address, public_key: wallet.public_key}
);

const signed = await globalThis.DenariusWalletCrypto.signTransaction(
  wallet,
  password,
  {
    recipient_address: wallet.address,
    amount: "1.25",
    fee: "0.0001",
    nonce: 7
  },
  {protocol_version: 3, network: "denarius-testnet-v3"}
);
assert.equal(signed.transaction.amount_atomic, "125000000");
assert.equal(signed.transaction.fee_atomic, "10000");
assert.equal(signed.transaction.nonce, 7);
assert.equal(signed.signature.length, 128);
assert.equal(signed.transaction_id.length, 64);

const imported = await globalThis.DenariusWalletCrypto.importBackup(JSON.stringify(wallet));
assert.equal(imported.address, wallet.address);
await assert.rejects(
  globalThis.DenariusWalletCrypto.signTransaction(
    wallet,
    "incorrect wallet password",
    {recipient_address: wallet.address, amount: "1", fee: "0.0001", nonce: 0},
    {protocol_version: 3, network: "denarius-testnet-v3"}
  ),
  /Invalid wallet password/
);
