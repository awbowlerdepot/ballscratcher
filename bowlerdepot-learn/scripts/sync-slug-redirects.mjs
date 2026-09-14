#!/usr/bin/env node
// 036_product_articles_slug.sql / Al: "can we make the slugs for the
// pages more human readable... " -> "301 redirect old -> new
// (Recommended)" for what happens to old bare-uuid /articles/<id>/
// URLs. scripts/prerender.ts (run by `npm run build`, right before
// this script in .github/workflows/deploy-learn-site.yml) writes
// redirect-map.json at the bowlerdepot-learn/ project root -- an array
// of {"Key": "<product_id>", "Value": "<slug>"} pairs for every
// approved article that currently HAS a slug. This script pushes that
// mapping into the CloudFront KeyValueStore template.yaml's
// LearnArticleSlugRedirectsStore resource creates, which template.yaml's
// LearnSitePrettyUrlFunction (a CloudFront Function on viewer-request)
// reads at the edge to issue a real 301 for an old uuid URL -- see
// that resource's own comment for the full mechanics/why-a-KVS-not-
// Lambda@Edge reasoning.
//
// Deliberately a standalone script using the AWS CLI (via child_process),
// not the AWS SDK for JavaScript -- this repo's GitHub Actions runner
// already has the CLI preinstalled and already authenticated (the
// `Configure AWS credentials` step earlier in deploy-learn-site.yml
// exports the same env vars this script's child `aws` calls inherit),
// so this needed zero new npm dependencies.
//
// WHY BATCHED, NOT ONE UpdateKeys CALL: CloudFront KeyValueStore's
// UpdateKeys action caps the number of puts+deletes per call (see AWS's
// own CloudFront KeyValueStore documentation for the current limit) --
// this catalog's article count is expected to grow well past a small
// per-call batch size over time, so this chunks the mapping and issues
// one UpdateKeys call per chunk, re-fetching the store's ETag before
// each call (UpdateKeys is optimistic-concurrency: the ETag from the
// PREVIOUS call is stale the instant that call succeeds).
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REDIRECT_MAP_PATH = join(__dirname, "..", "redirect-map.json");

// Conservative -- comfortably under every batch limit CloudFront KVS's
// UpdateKeys has ever documented (50 as of this store's introduction).
// Safe to raise later if AWS raises the limit and this catalog's article
// count ever makes batching itself slow.
const BATCH_SIZE = 50;

function awsCli(args) {
  return execFileSync("aws", args, { encoding: "utf-8" });
}

function describeEtag(kvsArn) {
  const out = awsCli([
    "cloudfront-keyvaluestore",
    "describe-key-value-store",
    "--kvs-arn",
    kvsArn,
  ]);
  const parsed = JSON.parse(out);
  return parsed.ETag;
}

function updateKeys(kvsArn, etag, puts) {
  awsCli([
    "cloudfront-keyvaluestore",
    "update-keys",
    "--kvs-arn",
    kvsArn,
    "--if-match",
    etag,
    "--puts",
    JSON.stringify(puts),
  ]);
}

function chunk(items, size) {
  const chunks = [];
  for (let i = 0; i < items.length; i += size) {
    chunks.push(items.slice(i, i + size));
  }
  return chunks;
}

function main() {
  const kvsArn = process.env.LEARN_ARTICLE_SLUG_KVS_ARN;
  if (!kvsArn) {
    console.error(
      "LEARN_ARTICLE_SLUG_KVS_ARN is not set -- see template.yaml's LearnArticleSlugRedirectsStoreArn output for the value to put in this GitHub Actions repo variable.",
    );
    process.exit(1);
  }

  let entries;
  try {
    entries = JSON.parse(readFileSync(REDIRECT_MAP_PATH, "utf-8"));
  } catch (err) {
    console.error(`Couldn't read/parse ${REDIRECT_MAP_PATH} -- did scripts/prerender.ts run first?`, err);
    process.exit(1);
  }

  if (entries.length === 0) {
    console.log("redirect-map.json has no entries yet (no approved article has a slug) -- nothing to sync.");
    return;
  }

  const batches = chunk(entries, BATCH_SIZE);
  let synced = 0;
  for (const batch of batches) {
    // Fresh ETag before EVERY batch -- the previous UpdateKeys call (if
    // any) already advanced it, so reusing an old one would 412.
    const etag = describeEtag(kvsArn);
    updateKeys(kvsArn, etag, batch);
    synced += batch.length;
  }

  console.log(`Synced ${synced} product_id -> slug redirect entr${synced === 1 ? "y" : "ies"} in ${batches.length} batch(es).`);
}

main();
