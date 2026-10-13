#!/bin/bash
# 把 scratch v2/ 的內容填進 repo 分支並推送
S=${SCRATCH:?請先 export SCRATCH=<你的 scratch 目錄>}
cd /home/claude/taiwan-stock-vault || exit 1
git fetch -q origin main && git merge -q origin/main -m "合併 main" || { echo MERGE_FAIL; exit 1; }
python3 $S/fixlinks.py . $S/v2 >> $S/fixlinks_log.txt
python3 scripts/fill_notes.py . $S/v2 --replace > $S/fill_log.txt
grep -v '填入 無' $S/fill_log.txt | grep -v -E '^\S+: 填入 ch1,ch2,ch3,ch4,chain,termsblock' | head -5
n=$(git status --porcelain -- 個股 | wc -l)
[ "$n" = 0 ] && echo "沒有新變更"
[ "$n" != 0 ] && git add -A 個股 名詞 && git commit -qm "撰寫個股內文 v2：新增 $n 檔

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HDxXktas31mFho7fRwH2LJ" && git push -q origin claude/project-thread-hqxpyj 2>&1 | tail -1
echo "推送 $n 檔；分支上已寫內文 $(grep -l '^內文更新:' 個股/台股/*.md | wc -l) 檔"
# 使用者同意直接寫 main（2026-10-07 14:32Z）：推到 main，排程若剛推過就先合併再重試
for i in 1 2 3 4; do
  git push -q origin HEAD:main 2>/dev/null && { echo "main 已更新到 $(git rev-parse --short HEAD)"; break; }
  sleep $((2**i)); git fetch -q origin main && git merge -q origin/main -m "合併 main" || { echo MERGE_FAIL; exit 1; }
  git push -q origin claude/project-thread-hqxpyj
done
