// Tăng cường giao diện. Không có JS thì mọi form vẫn gửi bình thường.
(() => {
  document.documentElement.classList.add('js');
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const mb = (n) => n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' KB'
    : (n / 1048576).toLocaleString('vi-VN', { maximumFractionDigits: 1 }) + ' MB';
  const icon = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  // Đóng thông báo
  $$('[data-dismiss]').forEach((b) => b.addEventListener('click', () => b.closest('.flash').remove()));

  // Hỏi lại trước khi xoá
  $$('form[data-confirm]').forEach((f) => f.addEventListener('submit', (e) => {
    if (!confirm(f.dataset.confirm)) e.preventDefault();
  }));

  // Cảnh báo khi rời trang mà chưa lưu thông tin đã gõ
  let dirty = false;
  let uploading = false;
  let picked = 0; // số ảnh đã chọn nhưng chưa tải lên, ở mọi ô chọn ảnh
  const guard = $('form[data-dirty-guard]');
  if (guard) {
    guard.addEventListener('input', (e) => { dirty = true; e.target.dataset.touched = '1'; });
    guard.addEventListener('submit', () => { dirty = false; });
  }
  window.addEventListener('beforeunload', (e) => {
    if (dirty || uploading) { e.preventDefault(); e.returnValue = ''; }
  });

  // ---------- Chọn ảnh: xem trước, bỏ bớt, kéo thả, thanh % ----------
  $$('form[data-uploader]').forEach((form) => {
    const input = $('input[type=file]', form);
    const drop = $('.drop', form);
    const list = $('.previews', form);
    const sum = $('.upl-sum', form);
    const barWrap = $('.upl-bar', form);
    const bar = $('.bar span', form);
    const pct = $('.upl-pct', form);
    const err = $('.upl-err', form);
    const btn = $('button[type=submit]', form);
    const btnT = $('.btn-t', btn);
    const label = btnT.textContent;
    const have = +form.dataset.have, max = +form.dataset.max, min = +form.dataset.min;
    const valid = +(form.dataset.valid || 0); // số ảnh đang hợp lệ: dùng cho câu báo tiền
    const LIMIT = 20 * 1024 * 1024;
    let files = [];

    input.required = false; // JS tự kiểm, để còn báo lỗi tiếng Việt
    btn.disabled = true;

    const showErr = (msg) => { err.hidden = !msg; err.innerHTML = msg ? icon('alert') + `<span></span>` : ''; if (msg) $('span', err).textContent = msg; };

    function render() {
      $$('img', list).forEach((i) => URL.revokeObjectURL(i.src));
      list.textContent = '';
      files.forEach((f, i) => {
        const li = document.createElement('li');
        const img = new Image();
        img.alt = '';
        img.decoding = 'async';
        img.src = URL.createObjectURL(f);
        const size = document.createElement('span');
        size.className = 'pv-size';
        size.textContent = mb(f.size);
        const rm = document.createElement('button');
        rm.type = 'button';
        rm.className = 'icon-btn pv-rm';
        rm.setAttribute('aria-label', `Bỏ ảnh ${i + 1} (${f.name})`);
        rm.innerHTML = icon('x');
        rm.addEventListener('click', () => { files.splice(i, 1); render(); (files.length ? $('.pv-rm', list) : drop).focus(); });
        li.append(img, size, rm);
        list.append(li);
      });
      picked = $$('.previews li').length;
      list.hidden = !files.length;
      btn.disabled = !files.length;
      sum.hidden = !files.length;
      if (!files.length) return;
      const total = valid + files.length;
      const bytes = files.reduce((s, f) => s + f.size, 0);
      sum.textContent = `Đã chọn ${files.length} ảnh (${mb(bytes)}). ` + (total < min
        ? `Hồ sơ cần ít nhất ${min} ảnh hợp lệ, bạn có thể chọn thêm hoặc bổ sung sau.`
        : `Nếu ${have ? 'các ảnh mới đều' : 'tất cả'} hợp lệ, hồ sơ được ${total > min ? '50.000đ' : '35.000đ'}.`);
    }

    function add(picked) {
      const msgs = [];
      for (const f of picked) {
        if (!/^image\/(jpeg|png)$/.test(f.type) && !/\.(jpe?g|png)$/i.test(f.name)) { msgs.push(`${f.name}: chỉ nhận ảnh JPG hoặc PNG`); continue; }
        if (f.size > LIMIT) { msgs.push(`${f.name}: ảnh quá 20 MB`); continue; }
        if (files.some((x) => x.name === f.name && x.size === f.size && x.lastModified === f.lastModified)) continue;
        if (have + files.length >= max) { msgs.push(`Mỗi hồ sơ tối đa ${max} ảnh, đã bỏ bớt ảnh thừa`); break; }
        files.push(f);
      }
      showErr(msgs.join('. '));
      render();
    }

    input.addEventListener('change', () => { add(input.files); input.value = ''; });
    ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('over')));
    drop.addEventListener('drop', (e) => { e.preventDefault(); add(e.dataTransfer.files); });

    function lock(on) {
      uploading = on;
      btn.disabled = on;
      input.disabled = on;
      form.classList.toggle('busy', on);
      btnT.textContent = on ? 'Đang tải ảnh…' : label;
      barWrap.hidden = !on;
    }
    function fail(msg) { lock(false); showErr(msg); }

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if (!files.length) { showErr('Chọn ít nhất 1 ảnh.'); return; }
      const fd = new FormData();
      files.forEach((f) => fd.append('files', f, f.name));
      const x = new XMLHttpRequest();
      x.open('POST', form.action);
      x.setRequestHeader('X-Upload', '1');
      x.upload.addEventListener('progress', (ev) => {
        if (!ev.lengthComputable) return;
        const p = Math.round((ev.loaded / ev.total) * 100);
        bar.style.width = p + '%';
        pct.textContent = `Đang tải lên ${mb(ev.loaded)} / ${mb(ev.total)} (${p}%)`;
      });
      x.upload.addEventListener('load', () => {
        bar.style.width = '100%';
        barWrap.classList.add('busy');
        pct.textContent = 'Đã tải xong. Đang kiểm tra giờ chụp, vị trí và ảnh trùng…';
      });
      x.addEventListener('load', () => {
        if (x.status >= 400) return fail(`Tải lên không thành công (mã ${x.status}). Vui lòng thử lại.`);
        let url = x.responseURL;
        try { url = JSON.parse(x.responseText).url || url; } catch { /* bị chuyển trang (vd hết phiên) */ }
        uploading = false;
        picked = 0;
        const to = new URL(url, location.href);
        if (to.pathname + to.search === location.pathname + location.search) {
          location.hash = to.hash; // chỉ đổi #... thì trình duyệt không tải lại, phải tự tải lại
          location.reload();
        } else location.href = to.href;
      });
      x.addEventListener('error', () => fail('Mất kết nối khi tải ảnh. Kiểm tra mạng rồi bấm tải lại.'));
      showErr('');
      lock(true);
      bar.style.width = '0%';
      pct.textContent = 'Bắt đầu tải lên…';
      x.send(fd);
    });
  });

  // ---------- Xem ảnh lớn ----------
  const dlg = $('dialog.lightbox');
  const shots = $$('[data-lightbox]');
  if (dlg && shots.length) {
    const img = $('img', dlg);
    let cur = 0;
    const show = (k) => {
      cur = (k + shots.length) % shots.length;
      const d = shots[cur].dataset;
      img.src = d.src;
      img.alt = `Ảnh ${d.n}`;
      $('.lb-n', dlg).textContent = `Ảnh ${d.n} / ${shots.length}`;
      $('.lb-full', dlg).href = d.full;
      $('.lb-time', dlg).textContent = d.time;
      $('.lb-place', dlg).textContent = d.place;
      const map = $('.lb-map', dlg);
      map.hidden = !d.map;
      if (d.map) map.href = d.map;
      const bad = $('.lb-bad', dlg);
      const probs = d.bad ? d.bad.split('|') : [];
      bad.innerHTML = probs.length
        ? probs.map(() => `<li class="bad-text">${icon('x')}<span></span></li>`).join('')
        : `<li class="ok-text">${icon('check')}<span>Ảnh hợp lệ</span></li>`;
      probs.forEach((p, i) => { $$('span', bad)[i].textContent = p; });
      $('.lb-prev', dlg).hidden = $('.lb-next', dlg).hidden = shots.length < 2;
    };
    shots.forEach((b, k) => b.addEventListener('click', () => { show(k); dlg.showModal(); }));
    $('.lb-prev', dlg).addEventListener('click', () => show(cur - 1));
    $('.lb-next', dlg).addEventListener('click', () => show(cur + 1));
    $('.lb-close', dlg).addEventListener('click', () => dlg.close());
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    dlg.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft') show(cur - 1);
      if (e.key === 'ArrowRight') show(cur + 1);
    });
    dlg.addEventListener('close', () => shots[cur].focus());
    let x0 = null;
    img.addEventListener('touchstart', (e) => { x0 = e.touches[0].clientX; }, { passive: true });
    img.addEventListener('touchend', (e) => {
      if (x0 === null) return;
      const dx = e.changedTouches[0].clientX - x0;
      if (Math.abs(dx) > 50) show(cur + (dx < 0 ? 1 : -1));
      x0 = null;
    });
  }

  // ---------- Chờ AI đọc ảnh ----------
  const poll = $('[data-ai-poll]');
  if (poll) {
    const tick = async () => {
      try {
        const r = await fetch(poll.dataset.aiPoll, { headers: { Accept: 'application/json' } });
        const j = await r.json();
        if (j.ai_state !== 'dang_doc') {
          if (uploading) { setTimeout(tick, 3000); return; } // đang tải ảnh: đợi xong đã
          const busy = dirty || picked || $('dialog[open]');
          if (!busy) { location.reload(); return; }
          if (!guard) { // không có form thông tin: chỉ báo, để người dùng tự tải lại
            poll.className = 'alert ok';
            poll.innerHTML = `${icon('check')}<div><strong>AI đã đọc xong ảnh.</strong><p><a href="">Tải lại trang</a> để xem kết quả.</p></div>`;
            return;
          }
          // Đang gõ dở: chỉ điền vào ô còn trống mà người dùng chưa đụng, không tải lại trang
          let n = 0;
          for (const [k, v] of Object.entries(j.fields || {})) {
            if (!v) continue;
            if (k === 'stage') {
              const radios = $$('input[name=stage]', guard);
              if (!radios.some((r) => r.checked || r.dataset.touched)) {
                const r = radios.find((r) => r.value === v);
                if (r) { r.checked = true; n++; }
              }
              continue;
            }
            const el = guard.elements[k];
            if (el && !el.value && !el.dataset.touched) { el.value = v; n++; }
          }
          poll.className = 'alert ok';
          poll.innerHTML = `${icon('check')}<div><strong>AI đã đọc xong ảnh.</strong><p></p></div>`;
          $('p', poll).textContent = (n ? `Đã điền thêm ${n} ô còn trống, bạn kiểm tra lại. ` : '') +
            'Bấm Lưu nháp để lưu và xem kết quả kiểm tra từng ảnh.';
          return;
        }
      } catch { /* mạng chập chờn: thử lại */ }
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  }
})();
