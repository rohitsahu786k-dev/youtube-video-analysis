import type { NextConfig } from "next";
const config: NextConfig = { poweredByHeader:false, serverExternalPackages:["bullmq","ioredis"], images:{remotePatterns:[{protocol:"https",hostname:"i.ytimg.com"},{protocol:"https",hostname:"img.youtube.com"}]}};
export default config;
