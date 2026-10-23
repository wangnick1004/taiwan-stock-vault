// 把 SVG 轉成 2 倍解析度 PNG（需 playwright）。用法：node scripts/0050總覽/svg2png.js 0050總覽文/附件/*.svg
const {chromium}=require('playwright');const fs=require('fs');const path=require('path');
(async()=>{const b=await chromium.launch();const p=await b.newPage({deviceScaleFactor:2});
for(const f of process.argv.slice(2)){const s=fs.readFileSync(f,'utf8');const m=s.match(/width="(\d+)" height="(\d+)"/);
await p.setViewportSize({width:+m[1],height:+m[2]});await p.setContent(`<html><body style="margin:0">${s}</body></html>`);
await p.screenshot({path:f.replace(/\.svg$/,'.png')});}await b.close();})();
