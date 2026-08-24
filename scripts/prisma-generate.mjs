import { spawnSync } from "node:child_process";
if(!process.env.DATABASE_URL) process.env.DATABASE_URL="postgresql://preview:preview@localhost:5432/preview";
const bin=process.platform==="win32"?"node_modules\\.bin\\prisma.cmd":"node_modules/.bin/prisma";
const r=spawnSync(bin,["generate"],{stdio:"inherit",env:process.env});
process.exit(r.status??1);
