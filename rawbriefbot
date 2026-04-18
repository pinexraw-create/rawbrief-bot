#!/usr/bin/env python3
"""
Crypto, Stock & Commodity News Bot
Fetches financial news via NewsAPI, formats with Claude Haiku, sends to Telegram.
Setup requirements:
  1. NEWS_API_KEY       — from https://newsapi.org
  2. ANTHROPIC_API_KEY  — from https://console.anthropic.com
  3. TELEGRAM_BOT_TOKEN — from @BotFather on Telegram
  4. TELEGRAM_CHANNEL_ID — channel username (e.g. @mychannel) or numeric ID
  The bot MUST be added as an Administrator to your Telegram channel before running.
Run:
  python3 crypto_news_bot.py
"""
import os
import sys
import time
import requests
import anthropic
import urllib.request
import urllib.error
import json
from datetime import datetime, timedelta
NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
def check_env():
    missing = []
    for var in ["NEWS_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        print(f"  ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
def verify_telegram_bot() -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                bot = result["result"]
                print(f"  Bot verified: @{bot['username']} ({bot['first_name']})")
                return bot
            else:
                print(f"  ERROR: Invalid bot token — {result.get('description')}")
                sys.exit(1)
    except Exception as e:
        print(f"  ERROR: Could not reach Telegram API — {e}")
        sys.exit(1)
def fetch_prices() -> dict:
    """Fetch live prices for BTC, S&P 500, and Gold."""
    prices = {}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        btc_price = data["bitcoin"]["usd"]
        btc_change = data["bitcoin"]["usd_24h_change"]
        sign = "+" if btc_change >= 0 else ""
        prices["BTC"] = f"${btc_price:,.0f} ({sign}{btc_change:.1f}%)"
    except Exception as e:
        print(f"  WARNING: Could not fetch BTC price: {e}")
        prices["BTC"] = "N/A"
    for ticker, label in [("%5EGSPC", "SPX"), ("GC%3DF", "Gold")]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            meta = data["chart"]["result"][0]["meta"]
            price = meta["regularMarketPrice"]
            prev_close = meta["previousClose"]
            change_pct = ((price - prev_close) / prev_close) * 100
            sign = "+" if change_pct >= 0 else ""
            if label == "Gold":
                prices["Gold"] = f"${price:,.0f}/oz ({sign}{change_pct:.1f}%)"
            else:
                prices["SPX"] = f"{price:,.0f} ({sign}{change_pct:.1f}%)"
        except Exception as e:
            print(f"  WARNING: Could not fetch {label} price: {e}")
            prices[label] = "N/A"
    print(f"  BTC: {prices.get('BTC')} | S&P 500: {prices.get('SPX')} | Gold: {prices.get('Gold')}")
    return prices
def fetch_fear_greed() -> str:
    """Fetch the Crypto Fear & Greed Index from alternative.me."""
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        entry = data["data"][0]
        value = entry["value"]
        classification = entry["value_classification"]
        print(f"  Fear & Greed Index: {value} ({classification})")
        return f"{value}/100 — {classification}"
    except Exception as e:
        print(f"  WARNING: Could not fetch Fear & Greed Index: {e}")
        return "N/A"
def fetch_news() -> list[dict]:
    """Fetch crypto, stock, and commodity news from NewsAPI."""
    base_url = "https://newsapi.org/v2/everything"
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    queries = [
        ('bitcoin OR ethereum OR "bitcoin ETF" OR "ethereum ETF" OR "crypto ETF" OR BTC OR ETH OR "crypto regulation"', "crypto"),
        ('"S&P 500" OR Nasdaq OR Apple OR Tesla OR Microsoft OR Google OR Meta OR Nvidia OR Amazon OR JPMorgan OR "Goldman Sachs" OR "Morgan Stanley" OR earnings', "stocks"),
        ('"gold price" OR "silver price" OR "WTI" OR "Brent crude" OR "crude oil" OR "natural gas price" OR "wheat price" OR "copper price"', "commodities"),
    ]
    all_articles = []
    for query, category in queries:
        params = {
            "q": query,
            "from": yesterday,
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": 7,
            "apiKey": NEWS_API_KEY,
        }
        try:
            response = requests.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "error":
                print(f"  WARNING: NewsAPI error for {category}: {data.get('message')}")
                continue
            articles = data.get("articles", [])
            count = 0
            for article in articles:
                if article.get("title") and article.get("description"):
                    if article["title"] == "[Removed]":
                        continue
                    all_articles.append({
                        "category": category,
                        "title": article["title"],
                        "description": article.get("description", ""),
                        "url": article.get("url", ""),
                        "source": article.get("source", {}).get("name", "Unknown"),
                        "publishedAt": article.get("publishedAt", ""),
                    })
                    count += 1
                    if count >= 6:
                        break
            print(f"  [{category.upper()}] Fetched {count} articles")
        except requests.RequestException as e:
            print(f"  WARNING: Failed to fetch {category} news: {e}")
    return all_articles
def format_with_claude(articles: list[dict], prices: dict, fear_greed: str) -> str:
    """Use Claude Haiku to format articles into a punchy Telegram post."""
    if not articles:
        return None
    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += (
            f"{i}. [{article['category'].upper()}] {article['title']}\n"
            f"   Summary: {article['description']}\n\n"
        )
    today = datetime.utcnow().strftime("%B %d, %Y")
    btc = prices.get("BTC", "N/A")
    spx = prices.get("SPX", "N/A")
    gold = prices.get("Gold", "N/A")
    prompt = f"""You are a financial news editor for a beginner-friendly Telegram channel. Produce today's post using EXACTLY the template below.
TEMPLATE (copy structure exactly, fill in bullets from the articles):
🌍 Raw Brief — {today}
——————————————————
🟠 BTC {btc}
📊 S&P 500 {spx}
🟡 Gold {gold}
😱 Fear & Greed {fear_greed}
——————————————————
🪙 Crypto
→ [bullet] [emoji]
→ [bullet] [emoji]
——————————————————
📈 Stocks
→ [bullet] [emoji]
→ [bullet] [emoji]
——————————————————
🛢 Commodities
→ [bullet] [emoji]
→ [bullet] [emoji]
——————————————————
Not financial advice. DYOR. 🌲
BULLET RULES — follow every one:
1. Exactly 2 bullets per section, never more or fewer
2. Every bullet MUST include a specific number, price, or % — no vague statements ever
3. Emoji goes at the END: '→ Text here 🚀' — never at the start
4. Emojis: 🚀 bullish, 🔴 bearish, 👀 watch, ⚠️ risk, 📉 down, 📈 up
5. Max 15 words per bullet
6. If a company or term may be unfamiliar to beginners, add 2–3 words of context in brackets immediately after the name. Examples:
   - 'Paradigm (crypto VC firm) dropped 40%' ⚠️
   - 'BlackRock (world's largest fund) bought $500M in Bitcoin' 🚀
   - 'Brent crude (global oil benchmark) rose to $92' 📈
   Do NOT add brackets for: Bitcoin, Ethereum, Apple, Tesla, Google, Meta, Nvidia, Amazon, Microsoft, S&P 500, Nasdaq, gold, oil, silver
TONE — this is the most important rule:
- Write like a sharp, confident friend who follows markets every day — not like a news wire or press release
- Short, punchy, natural. Think: texting a friend who wants the real take, not a summary
- Good examples:
  'Bitcoin quietly climbing — institutions loading up at $75K 🚀'
  'Oil spike incoming if Hormuz talks collapse ⚠️'
  'Nvidia up 4% — AI spending nowhere near slowing 📈'
  'Gold holding $4,800 — nobody trusts equities right now 👀'
- Bad examples (never write like this):
  'Bitcoin experienced a price increase amid institutional buying activity'
  'Oil prices may be impacted by geopolitical developments in the region'
- No corporate speak, no passive voice, no filler words
CONTENT RULES:
- Crypto: only Bitcoin, Ethereum, major crypto ETFs, or crypto regulation news — nothing else
- Stocks: only S&P 500 top companies (Apple, Tesla, Microsoft, Google, Meta, Nvidia, Amazon) or major bank earnings (JPMorgan, Goldman Sachs, Morgan Stanley) — filter out small caps, obscure companies, robotics firms, regional stocks
- Commodities: ONLY gold, silver, oil (Brent/WTI), natural gas, wheat, or copper — price movements only
  - Every commodities bullet MUST mention a specific price (e.g. $92/barrel, $3,200/oz) or % move
  - Reject any article about: regional holidays, small mining companies, commodity stocks, obscure producers, or any commodity not in the list above
- If an article does not fit these topics, ignore it and use another one
- No source names, no URLs, no links
- Plain text only — no asterisks, underscores, or Markdown
Articles:
{articles_text}
Write only the post. Nothing else."""
    base_url = os.environ.get("AI_INTEGRATIONS_ANTHROPIC_BASE_URL")
    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        **({"base_url": base_url} if base_url else {})
    )
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()
def send_to_telegram(text: str) -> bool:
    """Send the formatted message to the Telegram channel."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                msg_id = result["result"]["message_id"]
                print(f"  Message sent! Message ID: {msg_id}")
                return True
            else:
                desc = result.get("description", "Unknown error")
                print(f"  ERROR: Telegram rejected the message — {desc}")
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        error_data = json.loads(body) if body else {}
        desc = error_data.get("description", e.reason)
        print(f"  ERROR {e.code}: {desc}")
        if "chat not found" in desc.lower():
            print()
            print("  HINT: The bot must be an admin of the channel.")
            print("    Fix: Telegram channel > Edit > Administrators > Add your bot.")
        return False
    except Exception as e:
        print(f"  ERROR: Unexpected failure — {e}")
        return False
MAX_RETRIES = 10
RETRY_INTERVAL_SECONDS = 5 * 60  # 5 minutes
def _attempt_post() -> bool:
    """Run the full fetch → format → send pipeline. Returns True on success."""
    print("\n[1/5] Checking environment variables...")
    check_env()
    print("  All secrets present.")
    print("\n[2/5] Verifying Telegram bot...")
    verify_telegram_bot()
    print("\n[3/5] Fetching live data...")
    prices = fetch_prices()
    fear_greed = fetch_fear_greed()
    articles = fetch_news()
    if not articles:
        print("  No articles found.")
        return False
    print(f"  Total articles collected: {len(articles)}")
    print("\n[4/5] Formatting post with Claude Haiku...")
    post_text = format_with_claude(articles, prices, fear_greed)
    if not post_text:
        print("  Claude returned no content.")
        return False
    print("  Post formatted successfully.")
    print()
    print("─" * 55)
    print(post_text)
    print("─" * 55)
    print(f"  Character count: {len(post_text)}")
    print("\n[5/5] Sending to Telegram channel...")
    return send_to_telegram(post_text)
def run_bot():
    """Run one scheduled post with up to MAX_RETRIES retries (5 min apart)."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print("\n" + "=" * 55)
    print(f"   Running bot — {now}")
    print("=" * 55)
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            print(f"\n  ↻ Retry {attempt - 1}/{MAX_RETRIES - 1} "
                  f"— waiting 5 minutes before next attempt...")
            time.sleep(RETRY_INTERVAL_SECONDS)
        print(f"\n--- Attempt {attempt}/{MAX_RETRIES} "
              f"@ {datetime.utcnow().strftime('%H:%M UTC')} ---")
        try:
            success = _attempt_post()
        except Exception as e:
            print(f"  UNEXPECTED ERROR: {e}")
            success = False
        if success:
            print("\n" + "=" * 55)
            print("  Done! Post delivered to Telegram channel.")
            print("=" * 55)
            return
        print(f"  Attempt {attempt} failed.")
    print("\n" + "=" * 55)
    print(f"  All {MAX_RETRIES} attempts failed. Giving up until next scheduled time.")
    print("=" * 55)
def start_scheduler():
    """Start the APScheduler to post at 08:00, 13:00, and 20:00 UTC."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler = BlockingScheduler(timezone="UTC")
    for hour in [8, 13, 20]:
        scheduler.add_job(run_bot, "cron", hour=hour, minute=0)
        print(f"  Scheduled: {hour:02d}:00 UTC")
    print("\n" + "=" * 55)
    print("   Crypto News Bot — Scheduler Active")
    print("=" * 55)
    print("  Posts at: 08:00, 13:00, 20:00 UTC (London time)")
    print("  Press Ctrl+C to stop.")
    print("=" * 55)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n  Scheduler stopped.")
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Crypto News Telegram Bot")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()
    if args.once:
        run_bot()
    else:
        check_env()
        start_scheduler()
requirements.txt

requests==2.33.1
anthropic==0.96.0
APScheduler==3.11.2
.env.example

# Copy this file to .env and fill in your values
# (Railway: add these in the Variables tab instead)
NEWS_API_KEY=your_newsapi_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHANNEL_ID=@YourChannelUsername
Procfile

worker: python3 crypto_news_bot.py
package.json

{
  "name": "workspace",
  "version": "0.0.0",
  "license": "MIT",
  "scripts": {
    "preinstall": "sh -c 'rm -f package-lock.json yarn.lock; case \"$npm_config_user_agent\" in pnpm/*) ;; *) echo \"Use pnpm instead\" >&2; exit 1 ;; esac'",
    "build": "pnpm run typecheck && pnpm -r --if-present run build",
    "typecheck:libs": "tsc --build",
    "typecheck": "pnpm run typecheck:libs && pnpm -r --filter \"./artifacts/**\" --filter \"./scripts\" --if-present run typecheck"
  },
  "private": true,
  "devDependencies": {
    "typescript": "~5.9.2",
    "prettier": "^3.8.1"
  }
}
pnpm-workspace.yaml

minimumReleaseAge: 1440
minimumReleaseAgeExclude:
  - '@replit/*'
  - stripe-replit-sync
packages:
  - artifacts/*
  - lib/*
  - lib/integrations/*
  - scripts
catalog:
  '@replit/vite-plugin-cartographer': ^0.5.1
  '@replit/vite-plugin-dev-banner': ^0.1.1
  '@replit/vite-plugin-runtime-error-modal': ^0.0.6
  '@tailwindcss/vite': ^4.1.14
  '@tanstack/react-query': ^5.90.21
  '@types/node': ^25.3.3
  '@types/react': ^19.2.0
  '@types/react-dom': ^19.2.0
  '@vitejs/plugin-react': ^5.0.4
  class-variance-authority: ^0.7.1
  clsx: ^2.1.1
  drizzle-orm: ^0.45.2
  framer-motion: ^12.23.24
  lucide-react: ^0.545.0
  react: 19.1.0
  react-dom: 19.1.0
  tailwind-merge: ^3.3.1
  tailwindcss: ^4.1.14
  tsx: ^4.21.0
  vite: ^7.3.2
  zod: ^3.25.76
autoInstallPeers: false
onlyBuiltDependencies:
  - '@swc/core'
  - esbuild
  - msw
  - unrs-resolver
overrides:
  "@esbuild-kit/esm-loader": "npm:tsx@^4.21.0"
  esbuild: "0.27.3"
  brace-expansion: "^2.0.3"
  lodash: "^4.18.0"
  path-to-regexp: "^8.4.0"
  yaml: "^2.8.3"
  micromatch>picomatch: "^2.3.2"
  fdir>picomatch: "^4.0.4"
  vite>picomatch: "^4.0.4"
(Note: the full file includes all the platform-specific esbuild/rollup/lightningcss exclusion overrides — copy from the zip for the complete version)

tsconfig.base.json

{
  "compilerOptions": {
    "isolatedModules": true,
    "lib": ["es2022"],
    "module": "esnext",
    "moduleResolution": "bundler",
    "noEmitOnError": true,
    "noFallthroughCasesInSwitch": true,
    "noImplicitOverride": false,
    "noImplicitReturns": true,
    "noUnusedLocals": false,
    "noImplicitAny": true,
    "noImplicitThis": true,
    "strictNullChecks": true,
    "strictFunctionTypes": false,
    "strictBindCallApply": true,
    "strictPropertyInitialization": true,
    "useUnknownInCatchVariables": true,
    "alwaysStrict": true,
    "skipLibCheck": true,
    "target": "es2022",
    "types": [],
    "customConditions": ["workspace"]
  }
}
tsconfig.json

{
  "extends": "./tsconfig.base.json",
  "compileOnSave": false,
  "files": [],
  "references": [
    { "path": "./lib/db" },
    { "path": "./lib/api-client-react" },
    { "path": "./lib/api-zod" }
  ]
}
.npmrc

auto-install-peers=false
strict-peer-dependencies=false
artifacts/api-server/
package.json

{
  "name": "@workspace/api-server",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "export NODE_ENV=development && pnpm run build && pnpm run start",
    "build": "node ./build.mjs",
    "start": "node --enable-source-maps ./dist/index.mjs",
    "typecheck": "tsc -p tsconfig.json --noEmit"
  },
  "dependencies": {
    "@workspace/api-zod": "workspace:*",
    "@workspace/db": "workspace:*",
    "cookie-parser": "^1.4.7",
    "cors": "^2",
    "drizzle-orm": "catalog:",
    "express": "^5",
    "pino": "^9",
    "pino-http": "^10"
  },
  "devDependencies": {
    "@types/cookie-parser": "^1.4.10",
    "@types/cors": "^2.8.19",
    "@types/express": "^5.0.6",
    "@types/node": "catalog:",
    "esbuild": "^0.27.3",
    "esbuild-plugin-pino": "^2.3.3",
    "pino-pretty": "^13",
    "thread-stream": "3.1.0"
  }
}
tsconfig.json

{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "outDir": "dist",
    "rootDir": "src",
    "types": ["node"]
  },
  "include": ["src"],
  "references": [
    { "path": "../../lib/db" },
    { "path": "../../lib/api-zod" }
  ]
}
build.mjs

import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build as esbuild } from "esbuild";
import esbuildPluginPino from "esbuild-plugin-pino";
import { rm } from "node:fs/promises";
globalThis.require = createRequire(import.meta.url);
const artifactDir = path.dirname(fileURLToPath(import.meta.url));
async function buildAll() {
  const distDir = path.resolve(artifactDir, "dist");
  await rm(distDir, { recursive: true, force: true });
  await esbuild({
    entryPoints: [path.resolve(artifactDir, "src/index.ts")],
    platform: "node",
    bundle: true,
    format: "esm",
    outdir: distDir,
    outExtension: { ".js": ".mjs" },
    logLevel: "info",
    external: [
      "*.node", "sharp", "better-sqlite3", "sqlite3", "canvas", "bcrypt",
      "argon2", "fsevents", "re2", "farmhash", "xxhash-addon", "bufferutil",
      "utf-8-validate", "ssh2", "cpu-features", "dtrace-provider",
      "isolated-vm", "lightningcss", "pg-native", "oracledb",
      "mongodb-client-encryption", "nodemailer", "handlebars", "knex",
      "typeorm", "protobufjs", "onnxruntime-node", "@tensorflow/*",
      "@prisma/client", "@mikro-orm/*", "@grpc/*", "@swc/*", "@aws-sdk/*",
      "@azure/*", "@opentelemetry/*", "@google-cloud/*", "@google/*",
      "googleapis", "firebase-admin", "@parcel/watcher",
      "@sentry/profiling-node", "@tree-sitter/*", "aws-sdk", "classic-level",
      "dd-trace", "ffi-napi", "grpc", "hiredis", "kerberos", "leveldown",
      "miniflare", "mysql2", "newrelic", "odbc", "piscina", "realm",
      "ref-napi", "rocksdb", "sass-embedded", "sequelize", "serialport",
      "snappy", "tinypool", "usb", "workerd", "wrangler", "zeromq",
      "zeromq-prebuilt", "playwright", "puppeteer", "puppeteer-core", "electron",
    ],
    sourcemap: "linked",
    plugins: [esbuildPluginPino({ transports: ["pino-pretty"] })],
    banner: {
      js: `import { createRequire as __bannerCrReq } from 'node:module';
import __bannerPath from 'node:path';
import __bannerUrl from 'node:url';
globalThis.require = __bannerCrReq(import.meta.url);
globalThis.__filename = __bannerUrl.fileURLToPath(import.meta.url);
globalThis.__dirname = __bannerPath.dirname(globalThis.__filename);
    `,
    },
  });
}
buildAll().catch((err) => {
  console.error(err);
  process.exit(1);
});
src/index.ts

import app from "./app";
import { logger } from "./lib/logger";
const rawPort = process.env["PORT"];
if (!rawPort) {
  throw new Error("PORT environment variable is required but was not provided.");
}
const port = Number(rawPort);
if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}
app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }
  logger.info({ port }, "Server listening");
});
src/app.ts

import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import router from "./routes";
import { logger } from "./lib/logger";
const app: Express = express();
app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return { id: req.id, method: req.method, url: req.url?.split("?")[0] };
      },
      res(res) {
        return { statusCode: res.statusCode };
      },
    },
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use("/api", router);
export default app;
src/lib/logger.ts

import pino from "pino";
const isProduction = process.env.NODE_ENV === "production";
export const logger = pino({
  level: process.env.LOG_LEVEL ?? "info",
  redact: [
    "req.headers.authorization",
    "req.headers.cookie",
    "res.headers['set-cookie']",
  ],
  ...(isProduction
    ? {}
    : {
        transport: {
          target: "pino-pretty",
          options: { colorize: true },
        },
      }),
});
src/routes/index.ts

import { Router, type IRouter } from "express";
import healthRouter from "./health";
const router: IRouter = Router();
router.use(healthRouter);
export default router;
src/routes/health.ts

import { Router, type IRouter } from "express";
import { HealthCheckResponse } from "@workspace/api-zod";
const router: IRouter = Router();
router.get("/healthz", (_req, res) => {
  const data = HealthCheckResponse.parse({ status: "ok" });
  res.json(data);
});
export default router;
lib/api-spec/
package.json

{
  "name": "@workspace/api-spec",
  "version": "0.0.0",
  "private": true,
  "scripts": {
    "codegen": "orval --config ./orval.config.ts && pnpm -w run typecheck:libs"
  },
  "devDependencies": {
    "orval": "^8.5.2"
  }
}
openapi.yaml

openapi: 3.1.0
info:
  title: Api
  version: 0.1.0
  description: API specification
servers:
  - url: /api
    description: Base API path
tags:
  - name: health
    description: Health operations
paths:
  /healthz:
    get:
      operationId: healthCheck
      tags: [health]
      summary: Health check
      description: Returns server health status
      responses:
        "200":
          description: Healthy
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/HealthStatus"
components:
  schemas:
    HealthStatus:
      type: object
      properties:
        status:
          type: string
      required:
        - status
orval.config.ts

import { defineConfig, InputTransformerFn } from "orval";
import path from "path";
const root = path.resolve(__dirname, "..", "..");
const apiClientReactSrc = path.resolve(root, "lib", "api-client-react", "src");
const apiZodSrc = path.resolve(root, "lib", "api-zod", "src");
const titleTransformer: InputTransformerFn = (config) => {
  config.info ??= {};
  config.info.title = "Api";
  return config;
};
export default defineConfig({
  "api-client-react": {
    input: {
      target: "./openapi.yaml",
      override: { transformer: titleTransformer },
    },
    output: {
      workspace: apiClientReactSrc,
      target: "generated",
      client: "react-query",
      mode: "split",
      baseUrl: "/api",
      clean: true,
      prettier: true,
      override: {
        fetch: { includeHttpResponseReturnType: false },
        mutator: {
          path: path.resolve(apiClientReactSrc, "custom-fetch.ts"),
          name: "customFetch",
        },
      },
    },
  },
  zod: {
    input: {
      target: "./openapi.yaml",
      override: { transformer: titleTransformer },
    },
    output: {
      workspace: apiZodSrc,
      client: "zod",
      target: "generated",
      schemas: { path: "generated/types", type: "typescript" },
      mode: "split",
      clean: true,
      prettier: true,
      override: {
        zod: {
          coerce: {
            query: ['boolean', 'number', 'string'],
            param: ['boolean', 'number', 'string'],
            body: ['bigint', 'date'],
            response: ['bigint', 'date'],
          },
        },
        useDates: true,
        useBigInt: true,
      },
    },
  },
});
lib/api-zod/
package.json

{
  "name": "@workspace/api-zod",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "exports": {
    ".": "./src/index.ts"
  },
  "dependencies": {
    "zod": "catalog:"
  }
}
tsconfig.json

{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "composite": true,
    "declarationMap": true,
    "emitDeclarationOnly": true,
    "outDir": "dist",
    "rootDir": "src"
  },
  "include": ["src"]
}
src/index.ts

export * from "./generated/api";
export * from "./generated/types";
src/generated/api.ts

import * as zod from "zod";
export const HealthCheckResponse = zod.object({
  status: zod.string(),
});
src/generated/types/index.ts

export * from "./healthStatus";
src/generated/types/healthStatus.ts

export interface HealthStatus {
  status: string;
}
lib/db/
package.json

{
  "name": "@workspace/db",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "exports": {
    ".": "./src/index.ts",
    "./schema": "./src/schema/index.ts"
  },
  "scripts": {
    "push": "drizzle-kit push --config ./drizzle.config.ts",
    "push-force": "drizzle-kit push --force --config ./drizzle.config.ts"
  },
  "dependencies": {
    "drizzle-orm": "catalog:",
    "drizzle-zod": "^0.8.3",
    "pg": "^8.20.0",
    "zod": "catalog:"
  },
  "devDependencies": {
    "@types/node": "catalog:",
    "@types/pg": "^8.18.0",
    "drizzle-kit": "^0.31.9"
  }
}
tsconfig.json

{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "composite": true,
    "declarationMap": true,
    "emitDeclarationOnly": true,
    "outDir": "dist",
    "rootDir": "src",
    "types": ["node"]
  },
  "include": ["src"]
}
drizzle.config.ts

import { defineConfig } from "drizzle-kit";
import path from "path";
if (!process.env.DATABASE_URL) {
  throw new Error("DATABASE_URL, ensure the database is provisioned");
}
export default defineConfig({
  schema: path.join(__dirname, "./src/schema/index.ts"),
  dialect: "postgresql",
  dbCredentials: {
    url: process.env.DATABASE_URL,
  },
});
src/index.ts

import { drizzle } from "drizzle-orm/node-postgres";
import pg from "pg";
import * as schema from "./schema";
const { Pool } = pg;
if (!process.env.DATABASE_URL) {
  throw new Error("DATABASE_URL must be set. Did you forget to provision a database?");
}
export const pool = new Pool({ connectionString: process.env.DATABASE_URL });
export const db = drizzle(pool, { schema });
export * from "./schema";
src/schema/index.ts

// Export your models here. Add one export per file
// export * from "./posts";
export {}
lib/api-client-react/
package.json

{
  "name": "@workspace/api-client-react",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "exports": {
    ".": "./src/index.ts"
  },
  "dependencies": {
    "@tanstack/react-query": "catalog:"
  },
  "peerDependencies": {
    "react": ">=18"
  }
}
tsconfig.json

{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "composite": true,
    "declarationMap": true,
    "emitDeclarationOnly": true,
    "outDir": "dist",
    "rootDir": "src",
    "lib": ["dom", "es2022"]
  },
  "include": ["src"]
}
src/index.ts

export * from "./generated/api";
export * from "./generated/api.schemas";
export { setBaseUrl, setAuthTokenGetter } from "./custom-fetch";
export type { AuthTokenGetter } from "./custom-fetch";
src/generated/api.schemas.ts

export interface HealthStatus {
  status: string;
}
src/generated/api.ts — see the full content above (the useHealthCheck hook file, too long to repeat here but fully shown above)

src/custom-fetch.ts — see the full content above (the full fetch utility, too long to repeat but fully shown above)

scripts/
package.json

{
  "name": "@workspace/scripts",
  "version": "0.0.0",
  "private": true, **...**
_This response is too long to display in full._
