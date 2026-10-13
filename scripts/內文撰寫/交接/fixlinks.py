import re,os,sys,glob
repo,src=sys.argv[1],sys.argv[2]
names={f[:-3] for f in os.listdir(f"{repo}/個股/台股") if f.endswith('.md')}
allnames={os.path.basename(f)[:-3] for f in glob.glob(f"{repo}/**/*.md",recursive=True)}
bycode={}
for n in names:
    m=re.match(r"^(\d{4})",n)
    if m: bycode.setdefault(m.group(1),[]).append(n)
pat=re.compile(r"\[\[(\d{4}[^\]|#]*)((?:[|#][^\]]*)?)\]\]")
for p in glob.glob(f"{src}/*.md"):
    t=open(p).read(); log=[]
    def rep(m):
        n,rest=m.group(1),m.group(2)
        if n in names or n in allnames: return m.group(0)
        c=bycode.get(n[:4],[])
        if len(c)==1:
            log.append(f"{n}->{c[0]}"); return f"[[{c[0]}{rest}]]"
        log.append(f"{n}->plain"); return rest[1:] if rest.startswith('|') else n
    t2=pat.sub(rep,t)
    if t2!=t: open(p,'w').write(t2); print(os.path.basename(p),log)

# 使用者自己寫了核心供應鏈的筆記：內容檔不得帶 @@chain，避免 --replace 覆寫
PROTECT="2330 2454".split()
for c in PROTECT:
    p=f"{src}/{c}.md"
    if not os.path.exists(p): continue
    t=open(p).read()
    t2=re.sub(r"^@@chain\n.*?(?=^@@|\Z)","",t,flags=re.S|re.M)
    if t2!=t: open(p,"w").write(t2); print(c,"移除 @@chain")
