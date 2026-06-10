# ApexWealth — Vercel KV Setup Guide

## 1. Deploy to Vercel
Upload this zip via Vercel dashboard → New Project → Import zip

## 2. Create Vercel KV Store (free tier)
1. Go to your Vercel project dashboard
2. Click **Storage** tab → **Create Database** → choose **KV**
3. Name it `apexwealth-kv` → click **Create**
4. Click **Connect to Project** → select your ApexWealth project

## 3. Environment Variables (auto-set by Vercel KV)
Vercel automatically adds these to your project:
- `KV_REST_API_URL`
- `KV_REST_API_TOKEN`

You can verify them under **Project Settings → Environment Variables**

## 4. Redeploy
After connecting KV, trigger a redeploy:
Vercel Dashboard → Deployments → ⋯ → Redeploy

## 5. Verify KV is working
Visit: `https://your-app.vercel.app/api/health`
Expected response: `{"kv": "connected", "status": "ok"}`

## Local Development
```bash
pip install flask flask-cors yfinance upstash-redis
export KV_REST_API_URL="https://xxx.upstash.io"
export KV_REST_API_TOKEN="your-token"
cd api && python index.py
```
Get KV credentials from: Vercel Dashboard → Storage → your KV store → .env.local tab

## KV Key Schema
| Key | Contents |
|-----|----------|
| `user:{email}` | User account + hashed password |
| `holdings:{user_id}` | Array of portfolio holdings |
| `watchlist:{user_id}` | Array of watchlist items |
| `trades:{user_id}` | Array of completed trades |
