import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

// Standalone vector verifier. The worker does not import this module.
function embed(text) {
  if (typeof text !== "string" || !text.isWellFormed()) {
    throw new TypeError("Input must contain only Unicode scalar values");
  }
  const values = [];
  for (let counter = 0; counter !== 8; counter++) {
    const prefix = Buffer.alloc(4);
    prefix.writeUInt32BE(counter);
    const digest = createHash("sha256").update(prefix).update(text, "utf8").digest();
    for (let offset = 0; offset < digest.length; offset += 4) {
      values.push(digest.readInt32BE(offset) / 2147483648);
    }
  }
  let squared = 0;
  for (const value of values) squared += value * value;
  const norm = Math.sqrt(squared);
  return values.map(value => value / norm);
}

const texts = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(`${JSON.stringify(texts.map(text => ({ text, vector: embed(text) })))}\n`);
