(()=>{const notify=t=>{const n=document.querySelector('[role=status]');if(n)n.textContent=t;};
  // ---- chat export (PDF / Word / Markdown / Slides) - all client-side ----
  function chatMessages() {
    return [...document.querySelector('#log').querySelectorAll('.msg')].map((r) => ({
      role: r.classList.contains('user') ? 'You' : 'Crayon',
      text: (()=>{const n=(r.querySelector('.bubble')||r).cloneNode(true);n.querySelectorAll('button,.message-actions,.message-time,.verified-data-card').forEach(x=>x.remove());n.querySelectorAll('br').forEach(x=>x.replaceWith(document.createTextNode('\n')));n.querySelectorAll('p,div,li,h1,h2,h3,h4,pre').forEach(x=>x.append(document.createTextNode('\n')));return n.textContent.trim();})(),
    })).filter((m) => m.text);
  }
  function saveBlob(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
  }
  const stamp = () => new Date().toISOString().slice(0, 16).replace(/[T:]/g, '-');
  function exportMarkdown() {
    const ms = chatMessages(); if (!ms.length) { notify('Nothing to export yet.'); return; }
    const body = ms.map((m) => '**' + m.role + ':** ' + m.text).join('\n\n');
    saveBlob(new Blob(['# Crayon chat\n\n' + body + '\n'], {type: 'text/markdown'}), 'crayon-chat-' + stamp() + '.md');
  }
  function exportPDF() {
    const ms = chatMessages(); if (!ms.length) { notify('Nothing to export yet.'); return; }
    const { jsPDF } = window.jspdf; const doc = new jsPDF({unit: 'pt'});
    const width=doc.internal.pageSize.getWidth(),height=doc.internal.pageSize.getHeight(),W=width-80;
    // Canvas uses browser script shaping for Unicode. Images preserve text appearance,
    // but PDF text is not selectable in that mode; Word/Markdown retain editable text.
    const unicode=ms.some(m=>/[^\x20-\x7e\n\r\t]/.test(m.text));let y=50,canvas,ctx;
    function fresh(){if(unicode){canvas=document.createElement('canvas');canvas.width=Math.ceil(width*2);canvas.height=Math.ceil(height*2);ctx=canvas.getContext('2d');ctx.scale(2,2);ctx.fillStyle='#fff';ctx.fillRect(0,0,width,height);ctx.fillStyle='#111';}y=50;}
    function flush(){if(unicode)doc.addImage(canvas.toDataURL('image/jpeg',0.92),'JPEG',0,0,width,height);}
    function line(t,bold=false,size=10){if(y+size+5>height-40){flush();doc.addPage();fresh();}if(unicode){ctx.font=(bold?'bold ':'')+size+'px sans-serif';ctx.fillText(t,40,y);}else{doc.setFont('helvetica',bold?'bold':'normal');doc.setFontSize(size);doc.text(t,40,y);}y+=size+4;}
    fresh();line('Crayon chat',true,16);y+=10;
    for(const m of ms){line(m.role+':',true);for(const para of m.text.split('\n')){if(unicode){ctx.font='10px sans-serif';let text='';for(const word of para.match(/\S+\s*/g)||[]){if(ctx.measureText(text+word).width>W&&text){line(text.trimEnd());text='';ctx.font='10px sans-serif';}if(ctx.measureText(word).width>W){const chars=typeof Intl.Segmenter==='function'?[...new Intl.Segmenter(undefined,{granularity:'grapheme'}).segment(word)].map(x=>x.segment):Array.from(word);for(const ch of chars){if(ctx.measureText(text+ch).width>W&&text){line(text);text='';ctx.font='10px sans-serif';}text+=ch;}}else text+=word;}line(text||' ');}else{doc.setFont('helvetica','normal');doc.setFontSize(10);for(const l of doc.splitTextToSize(para||' ',W))line(l);}}y+=10;}
    flush();doc.save('crayon-chat-'+stamp()+'.pdf');
    if(unicode)notify('Unicode PDF saved as page images. Use Word or Markdown for selectable text.');
  }
  async function exportDocx() {
    const ms = chatMessages(); if (!ms.length) { notify('Nothing to export yet.'); return; }
    const kids = [new docx.Paragraph({text: 'Crayon chat', heading: docx.HeadingLevel.HEADING_1})];
    for (const m of ms) {
      kids.push(new docx.Paragraph({children: [new docx.TextRun({text: m.role + ':', bold: true})], spacing: {before: 240}}));
      for (const para of m.text.split('\n')) kids.push(new docx.Paragraph(para));
    }
    const blob = await docx.Packer.toBlob(new docx.Document({sections: [{children: kids}]}));
    saveBlob(blob, 'crayon-chat-' + stamp() + '.docx');
  }
  async function exportSlides() {
    const ms = chatMessages().filter((m) => m.role === 'Crayon');
    if (!ms.length) { notify('Ask Crayon something first - slides are built from its last answer.'); return; }
    const lines = ms[ms.length - 1].text.split('\n').map((l) => l.trim()).filter(Boolean);
    const title = lines[0].replace(/^[#*\-\d. )]+/, '').slice(0, 90) || 'Crayon slides';
    const points = lines.slice(1).map((l) => l.replace(/^[#*\-\u2022\d. )]+/, '')).filter(Boolean);
    const p = new PptxGenJS();
    p.defineLayout({name: 'W', width: 10, height: 5.63}); p.layout = 'W';
    const t = p.addSlide();
    t.addText(title, {x: 0.6, y: 2.1, w: 8.8, fontSize: 32, bold: true, color: '121013', align: 'center'});
    t.addText('Made with Crayon', {x: 0.6, y: 4.9, w: 8.8, fontSize: 12, color: '888888', align: 'center'});
    for (let i = 0; i < points.length; i += 5) {
      const s = p.addSlide();
      const chunk = points.slice(i, i + 5);
      s.addText(chunk[0].slice(0, 60), {x: 0.6, y: 0.4, w: 8.8, fontSize: 22, bold: true, color: '121013'});
      s.addText(chunk.map((c) => ({text: c, options: {bullet: true, fontSize: 16, paraSpaceAfter: 10}})), {x: 0.8, y: 1.3, w: 8.4, h: 3.9, color: '333333'});
    }
    await p.writeFile({fileName: 'crayon-slides-' + stamp() + '.pptx'});
  }
  (function () {
    const btn = document.querySelector('#exportbtn'); if (!btn) return;
    const menu = document.createElement('div');
    menu.id = 'exportmenu'; menu.className = 'hide';
    menu.innerHTML = '<button data-f="pdf">PDF document</button><button data-f="docx">Word (.docx)</button><button data-f="md">Markdown (.md)</button><button data-f="pptx">Slides (.pptx)</button>';
    document.body.appendChild(menu);
    const FNS = {pdf: exportPDF, docx: exportDocx, md: exportMarkdown, pptx: exportSlides};
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const r = btn.getBoundingClientRect();
      menu.style.top = (r.bottom + 6) + 'px'; menu.style.right = (innerWidth - r.right) + 'px';
      menu.classList.toggle('hide');
    });
    menu.addEventListener('click', (e) => {
      const f = e.target && e.target.dataset && e.target.dataset.f; if (!f) return;
      menu.classList.add('hide');
      Promise.resolve(FNS[f]()).catch((err) => { console.error(err); notify('Export failed - ' + (err && err.message || 'try again.')); });
    });
    document.addEventListener('click', () => menu.classList.add('hide'));
  })();

})();
