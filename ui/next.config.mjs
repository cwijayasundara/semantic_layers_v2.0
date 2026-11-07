// The browser only talks to Next (same origin); Next proxies /api/* to the FastAPI backend.
const API_URL = process.env.API_URL || "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
export default {
  reactStrictMode: true,
  agentRules: false, // don't let `next dev` write an AGENTS.md into the project
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};
