/*
 * GUI control panel: settings, account, chats and start/stop.
 * Loaded only when index.html is served by the macOS app (gui_mode).
 */
layui.use(['element', 'layer'], function () {
  var $ = layui.$;
  var element = layui.element;
  var layer = layui.layer;

  var TAB_FILTER = 'telegram_media_downloader';
  var STATE_TEXT = {
    need_config: '请先完成设置',
    connecting: '正在连接 Telegram…',
    error: '连接失败',
    logged_out: '未登录',
    code_sent: '等待输入验证码',
    need_password: '等待输入两步验证密码',
    ready: '就绪',
    downloading: '正在下载',
    stopping: '正在停止…'
  };
  var TAB_FOR_STATE = {
    need_config: 'settings',
    connecting: 'account',
    error: 'account',
    logged_out: 'account',
    code_sent: 'account',
    need_password: 'account',
    downloading: 'downloading',
    stopping: 'downloading'
  };
  var TYPE_TEXT = { channel: '频道', supergroup: '超级群', group: '群组' };

  var token = new URLSearchParams(window.location.search).get('token') ||
    sessionStorage.getItem('tdl_token') || '';
  sessionStorage.setItem('tdl_token', token);
  // also covers the existing pause/continue POST in index.html
  $.ajaxSetup({ headers: { 'X-Token': token } });

  var status = null;
  var chatRows = [];
  var userPickedTab = false;
  var programmaticTab = false;

  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Normalizes an id/username for loose comparison: strings, trimmed, no
  // leading "@", case-insensitive. Used to match a saved chat (keyed by
  // numeric id or by username) against a dialog when the server could not
  // resolve it itself (e.g. its dialog cache was still empty).
  function normalizeKey(value) {
    return String(value === null || value === undefined ? '' : value).trim().replace(/^@/, '').toLowerCase();
  }

  // Pure merge: builds the chat-list rows from the live dialog list and the
  // saved config chats. Every saved chat is matched against THIS `dialogs`
  // array — by its resolved dialog_id, by its chat_id, or by normalized
  // username — regardless of whether dialog_id is null (the server may
  // have resolved dialog_id from its own cache even when this particular
  // dialogs fetch failed and came back empty here). Only a saved chat that
  // matches no dialog actually present in `dialogs` becomes its own
  // "extra" row; that keeps a saved chat visible (and checked) instead of
  // silently disappearing when the dialogs fetch fails or hasn't happened.
  function buildChatRows(dialogs, saved) {
    dialogs = dialogs || [];
    saved = saved || [];
    var checkedById = {};
    var monthById = {};
    var sizeById = {};
    var backupById = {};
    var extra = [];
    saved.forEach(function (c) {
      var normKey = normalizeKey(c.chat_id);
      var hasDialogId = c.dialog_id !== null && c.dialog_id !== undefined;
      var match = dialogs.filter(function (d) {
        return (hasDialogId && String(d.id) === String(c.dialog_id)) ||
          String(d.id) === String(c.chat_id) ||
          (d.username && normalizeKey(d.username) === normKey);
      })[0];
      if (match) {
        checkedById[String(match.id)] = true;
        monthById[String(match.id)] = c.start_month || '';
        sizeById[String(match.id)] = c.max_file_size_mb || '';
        backupById[String(match.id)] = { on: !!c.backup_to_saved, end: c.backup_end_month || '', configKey: c.chat_id };
      } else {
        extra.push({ key: c.chat_id, configKey: c.chat_id, title: c.title, type: '', username: '', checked: true, startMonth: c.start_month || '', maxSize: c.max_file_size_mb || '', backup: !!c.backup_to_saved, backupEnd: c.backup_end_month || '' });
      }
    });
    return extra.concat(dialogs.map(function (d) {
      var b = backupById[String(d.id)] || {};
      // configKey: how config.yaml names the chat, which keys the backup status
      return { key: d.id, configKey: b.configKey, title: d.title, type: d.type, username: d.username, checked: !!checkedById[String(d.id)], startMonth: monthById[String(d.id)] || '', maxSize: sizeById[String(d.id)] || '', backup: !!b.on, backupEnd: b.end || '' };
    }));
  }

  function toast(message, ok) {
    // layer.msg renders HTML, and messages can contain chat titles
    layer.msg(esc(message), { icon: ok ? 1 : 2, time: ok ? 2000 : 4000 });
  }

  function api(method, path, body, quiet) {
    return $.ajax({
      url: path,
      type: method,
      dataType: 'json',
      contentType: 'application/json',
      data: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (res) {
      return res.data;
    }, function (xhr) {
      var message = (xhr.responseJSON && xhr.responseJSON.error) || '请求失败，请稍后再试';
      if (!quiet) {
        toast(message, false);
      }
      return $.Deferred().reject(message).promise();
    });
  }

  // ---- tabs

  function switchTab(id) {
    programmaticTab = true;
    element.tabChange(TAB_FILTER, id);
    programmaticTab = false;
  }

  window.onGuiTabChange = function (id) {
    if (!programmaticTab) {
      userPickedTab = true;
    }
    if (id === 'chats' && chatRows.length === 0 && canListChats()) {
      loadChats(false);
    }
  };

  function canListChats() {
    return status && (status.state === 'ready' || status.state === 'downloading');
  }

  function defaultTab(s) {
    if (s.state === 'ready') {
      return s.chat_count ? 'downloading' : 'chats';
    }
    if (s.state === 'downloading' && s.phase === 'backup') {
      return 'uploading';
    }
    return TAB_FOR_STATE[s.state];
  }

  // ---- status

  function renderBar(s) {
    var text = STATE_TEXT[s.state] || s.state;
    if (s.me) {
      text += ' · ' + (s.me.username ? '@' + s.me.username : s.me.name);
    }
    if (s.state === 'downloading' && s.phase === 'backup') {
      // a backup-only run, or a download run whose chats are all done
      text = (s.progress.total ? '下载完成，' : '') + '正在上传到收藏 · ' + ((s.uploads && s.uploads.speed) || '0.00 B/s');
    } else if (s.state === 'downloading') {
      text += ' · 已处理 ' + s.progress.done + ' / ' + s.progress.total + ' 条消息' + ' · 已下载 ' + s.progress.downloaded + ' 个文件';
    }
    $('#gui_state').text(text);
    $('#gui_notice').text(s.error || s.notice || '').toggleClass('gui-error', !!s.error);
    var enabled = s.state === 'ready' || s.state === 'downloading';
    $('#btn_main')
      .text(s.state === 'downloading' ? '停止' : '开始下载')
      .prop('disabled', !enabled)
      .toggleClass('layui-btn-disabled', !enabled)
      .toggleClass('layui-btn-danger', s.state === 'downloading');
    // during a run the main button is the one Stop for both kinds
    var canBackup = s.state === 'ready';
    $('#btn_backup')
      .toggle(s.state !== 'downloading')
      .prop('disabled', !canBackup)
      .toggleClass('layui-btn-disabled', !canBackup);
    $('#download_state').toggle(s.state === 'downloading');
    if (s.state === 'downloading') {
      // Follow the server's global pause state instead of the button's own
      // last answer: after a stop while paused, the next run starts
      // unpaused, and a stale "continue" would make every click a no-op.
      // data-value is what index.html's click handler sends.
      $('#download_state')
        .text(s.paused ? '继续' : '暂停')
        .attr('data-value', s.paused ? 'continue' : 'pause');
    }
  }

  function renderAccount(s) {
    $('#panel_account [data-show]').each(function () {
      var states = this.getAttribute('data-show').split(' ');
      $(this).toggle(states.indexOf(s.state) >= 0);
    });
    $('#panel_account [data-error]').text(s.error || '');
    $('#password_hint').text(s.password_hint ? '密码提示：' + s.password_hint : '');
    $('#account_me').text(s.me ? s.me.name + (s.me.username ? ' (@' + s.me.username + ')' : '') : '');
  }

  // under each backed-up chat: which months are in Saved Messages, and what
  // is being sent right now
  function renderBackup(backup) {
    backup = backup || {};
    $('#chat_list .gui-backup-state').each(function () {
      var b = backup[this.getAttribute('data-backup-key')];
      var text = '';
      if (b) {
        text = b.synced.length ? '已同步到收藏：' + b.synced.join('、') : '还没有同步完成的月份';
        if (b.current) {
          text += ' · 正在同步 ' + b.current;
        }
      }
      $(this).text(text).toggle(!!text);
    });
  }

  // the 上传中 tab: like 下载中, one row per file with a progress bar
  function renderUploads(u) {
    u = u || { files: [], speed: '0.00 B/s', months: {} };
    $('#upload_speed_title').text(u.speed);
    var months = Object.keys(u.months || {}).map(function (k) { return u.months[k]; });
    $('#upload_summary').text(u.files.length
      ? '正在上传 ' + u.files.length + ' 个文件 · 总速度 ' + u.speed + (months.length ? ' · 本月进度 ' + months.join('、') : '')
      : (status && status.phase === 'backup' ? '正在检查收藏夹里已有的文件…' : '当前没有上传任务'));
    $('#upload_list').html(u.files.map(function (f) {
      var p = esc(String(f.progress));
      return '<tr><td>' + esc(f.chat) + '</td><td>' + esc(f.month) + '</td><td>' + esc(f.name) + '</td><td>' + esc(f.size) + '</td>' +
        '<td><div class="layui-progress layui-progress-big"><div class="layui-progress-bar layui-bg-blue" style="width: ' + p + '%;">' +
        '<span class="layui-progress-text">' + p + '%</span></div></div></td>' +
        '<td>' + esc(f.speed) + '</td></tr>';
    }).join(''));
  }

  // the 已跳过 tab: videos this run skipped per chat, duplicates and oversize
  function renderSkips(k) {
    k = k || { chats: [], duplicate: 0, oversize: 0 };
    $('#skip_summary').text(k.chats.length
      ? '本次运行跳过重复视频 ' + k.duplicate + ' 个，超过大小限制 ' + k.oversize + ' 个（超限的提高限制后下次运行会重新下载）'
      : '本次运行还没有跳过视频');
    $('#skip_list').html(k.chats.map(function (c) {
      return '<tr><td>' + esc(c.chat) + '</td><td>' + esc(String(c.duplicate)) + '</td><td>' + esc(c.duplicate_size) + '</td>' +
        '<td>' + esc(String(c.oversize)) + '</td><td>' + esc(c.oversize_size) + '</td></tr>';
    }).join(''));
  }

  function refreshStatus() {
    return api('GET', 'api/status', undefined, true).then(function (s) {
      var changed = !status || status.state !== s.state;
      status = s;
      renderBar(s);
      renderAccount(s);
      renderBackup(s.backup);
      renderUploads(s.uploads);
      renderSkips(s.skips);
      $('#settings_form :input').prop('disabled', s.state === 'downloading' || s.state === 'stopping');
      if (changed) {
        if (!userPickedTab || s.state === 'downloading') {
          switchTab(defaultTab(s));
        }
        if (s.state === 'ready') {
          loadChats(false);
        }
      }
    });
  }

  // ---- settings

  function toggleProxyFields() {
    $('#proxy_fields').toggle(!!$('#settings_form [name=proxy_scheme]').val());
  }

  function toggleDuplicateMinSize() {
    $('#duplicate_min_size_row').toggle($('#skip_duplicate_media').prop('checked'));
  }

  function fillSettings(c) {
    var f = $('#settings_form');
    var p = c.proxy || {};
    // shown on the chats page, saved together with the chat list
    $('#skip_duplicate_media').prop('checked', !!c.skip_duplicate_media);
    $('#duplicate_min_size_mb').val(c.duplicate_min_size_mb);
    toggleDuplicateMinSize();
    f.find('[name=api_id]').val(c.api_id);
    f.find('[name=api_hash]').val(c.api_hash);
    f.find('[name=save_path]').val(c.save_path);
    f.find('[name=max_download_task]').val(c.max_download_task);
    f.find('[name=max_backup_task]').val(c.max_backup_task);
    f.find('[name=media_types]').each(function () {
      this.checked = c.media_types.indexOf(this.value) >= 0;
    });
    f.find('[name=proxy_scheme]').val(p.scheme || '');
    f.find('[name=proxy_hostname]').val(p.hostname || '127.0.0.1');
    f.find('[name=proxy_port]').val(p.port || '');
    f.find('[name=proxy_username]').val(p.username || '');
    f.find('[name=proxy_password]').val(p.password || '');
    toggleProxyFields();
  }

  function collectSettings() {
    var f = $('#settings_form');
    var scheme = f.find('[name=proxy_scheme]').val();
    return {
      api_id: f.find('[name=api_id]').val(),
      api_hash: f.find('[name=api_hash]').val(),
      proxy: scheme ? {
        scheme: scheme,
        hostname: f.find('[name=proxy_hostname]').val(),
        port: f.find('[name=proxy_port]').val(),
        username: f.find('[name=proxy_username]').val(),
        password: f.find('[name=proxy_password]').val()
      } : null,
      save_path: f.find('[name=save_path]').val(),
      media_types: f.find('[name=media_types]:checked').map(function () { return this.value; }).get(),
      max_download_task: f.find('[name=max_download_task]').val(),
      max_backup_task: f.find('[name=max_backup_task]').val()
    };
  }

  function loadSettings() {
    return api('GET', 'api/config').then(fillSettings);
  }

  $('#settings_form [name=proxy_scheme]').on('change', toggleProxyFields);
  $('#btn_save_settings').on('click', function () {
    api('POST', 'api/config', collectSettings()).then(function () {
      toast('已保存', true);
      loadSettings();
      refreshStatus();
    });
  });

  // ---- account

  function sendCode() {
    api('POST', 'api/login/phone', { phone: $('#login_phone').val() }).then(function () {
      toast('验证码已发送', true);
      refreshStatus();
    });
  }

  $('#btn_send_code, #btn_resend').on('click', sendCode);
  $('#btn_sign_in').on('click', function () {
    api('POST', 'api/login/code', { code: $('#login_code').val() }).then(function () {
      $('#login_code').val('');
      refreshStatus();
    });
  });
  $('#btn_check_password').on('click', function () {
    api('POST', 'api/login/password', { password: $('#login_password').val() }).then(function () {
      $('#login_password').val('');
      refreshStatus();
    });
  });
  $('#btn_retry').on('click', function () {
    api('POST', 'api/retry').then(refreshStatus);
  });
  $('#btn_logout').on('click', function () {
    layer.confirm('确定退出登录吗？下次需要重新输入验证码。', function (index) {
      layer.close(index);
      api('POST', 'api/logout').then(function () {
        chatRows = [];
        renderChats();
        refreshStatus();
      });
    });
  });

  // ---- chats

  function selectedCount() {
    return chatRows.filter(function (r) { return r.checked; }).length;
  }

  function renderChats() {
    var keyword = ($('#chat_search').val() || '').toLowerCase();
    var html = chatRows.map(function (row, index) {
      var haystack = (row.title + ' ' + row.username).toLowerCase();
      if (keyword && haystack.indexOf(keyword) < 0) {
        return '';
      }
      return '<label class="gui-chat-row">' +
        '<input type="checkbox" data-index="' + index + '"' + (row.checked ? ' checked' : '') + '>' +
        '<span class="gui-chat-title">' + esc(row.title) + '</span>' +
        (row.type ? '<span class="layui-badge layui-bg-gray">' + esc(TYPE_TEXT[row.type] || row.type) + '</span>' : '') +
        (row.username ? '<span class="gui-tip">@' + esc(row.username) + '</span>' : '') +
        '<input class="layui-input gui-month" data-month-index="' + index + '" maxlength="7" ' +
        'placeholder="起始年月 如 2024-06" value="' + esc(row.startMonth || '') + '">' +
        '<input class="layui-input gui-size" type="number" min="0" data-size-index="' + index + '" ' +
        'placeholder="大小上限 MB" title="单个文件最大（MB），留空不限" value="' + esc(String(row.maxSize || '')) + '">' +
        '<span class="gui-backup"><input type="checkbox" data-backup-index="' + index + '"' + (row.backup ? ' checked' : '') + '> 备份</span>' +
        '<input class="layui-input gui-backup-month" data-backup-end-index="' + index + '" maxlength="7" ' +
        'placeholder="截止年月 如 2026-09" title="这个月及更早的月份，从最旧的开始传到收藏夹" value="' + esc(row.backupEnd || '') + '">' +
        '</label>' +
        '<div class="gui-tip gui-backup-state" data-backup-key="' + esc(String(row.configKey === undefined ? row.key : row.configKey)) + '"></div>';
    }).join('');
    $('#chat_list').html(html || '<p class="gui-tip">没有找到频道或群组。</p>');
    $('#chat_selected').text('已选 ' + selectedCount() + ' 个');
    renderBackup(status && status.backup);
  }

  var chatsLoadPromise = null;
  var pendingRefresh = false;

  // Dialogs first, then chats: current_chats() on the server matches saved
  // chats against its dialog cache, which is empty until list_dialogs
  // finishes. Fetching chats only after dialogs resolves avoids the race
  // where every saved chat comes back with dialog_id: null and gets
  // rendered as a duplicate "extra" row.
  function startChatsLoad(refresh) {
    var dialogsRequest = api('GET', 'api/dialogs' + (refresh ? '?refresh=1' : '')).then(
      function (dialogs) { return dialogs; },
      function () { return []; } // still show saved chats even if dialogs failed
    );
    var request = dialogsRequest.then(function (dialogs) {
      return api('GET', 'api/chats').then(function (saved) {
        chatRows = buildChatRows(dialogs, saved);
        renderChats();
      });
    });
    request.always(function () {
      chatsLoadPromise = null;
      if (pendingRefresh) {
        pendingRefresh = false;
        chatsLoadPromise = startChatsLoad(true);
      }
    });
    return request;
  }

  function loadChats(refresh) {
    if (!canListChats()) {
      return $.Deferred().resolve().promise();
    }
    // In-flight guard: a load already running sees every dialog change and
    // is about to render, so a second concurrent call (e.g. the tab-switch
    // and the status-refresh both wanting to load chats on first READY)
    // just piggybacks on it instead of firing another get_dialogs request.
    // A refresh request (?refresh=1, from the 刷新列表 button) must not be
    // swallowed this way though: it's queued and runs once the in-flight
    // load finishes, instead of being dropped or run concurrently with it.
    if (chatsLoadPromise) {
      if (refresh) {
        pendingRefresh = true;
      }
      return chatsLoadPromise;
    }
    chatsLoadPromise = startChatsLoad(refresh);
    return chatsLoadPromise;
  }

  $('#chat_search').on('input', renderChats);
  $('#chat_list').on('input change', 'input.gui-month', function () {
    chatRows[Number(this.getAttribute('data-month-index'))].startMonth = $.trim(this.value);
  });
  $('#chat_list').on('input change', 'input.gui-size', function () {
    chatRows[Number(this.getAttribute('data-size-index'))].maxSize = $.trim(this.value);
  });
  $('#chat_list').on('change', 'input[data-index]', function () {
    chatRows[Number(this.getAttribute('data-index'))].checked = this.checked;
    $('#chat_selected').text('已选 ' + selectedCount() + ' 个');
  });
  $('#chat_list').on('change', 'input[data-backup-index]', function () {
    chatRows[Number(this.getAttribute('data-backup-index'))].backup = this.checked;
  });
  $('#chat_list').on('input change', 'input.gui-backup-month', function () {
    chatRows[Number(this.getAttribute('data-backup-end-index'))].backupEnd = $.trim(this.value);
  });
  $('#btn_refresh_dialogs').on('click', function () {
    loadChats(true);
  });
  $('#btn_add_link').on('click', function () {
    api('POST', 'api/chats/resolve', { link: $('#chat_link').val() }).then(function (info) {
      // Match by id, or by normalized username: a chat saved (or added)
      // by username before it had a numeric id, e.g. an "extra" row keyed
      // "foo", must not turn into a second row once [添加] resolves it.
      var normUsername = info.username ? normalizeKey(info.username) : '';
      var existing = chatRows.filter(function (r) {
        return String(r.key) === String(info.id) ||
          (normUsername && (normalizeKey(r.key) === normUsername || normalizeKey(r.username) === normUsername));
      })[0];
      if (existing) {
        existing.key = info.id;
        existing.title = info.title;
        existing.type = info.type;
        existing.username = info.username;
        existing.checked = true;
      } else {
        chatRows.unshift({ key: info.id, title: info.title, type: info.type, username: info.username, checked: true, startMonth: '', maxSize: '' });
      }
      $('#chat_link').val('');
      renderChats();
      toast('已添加「' + info.title + '」，记得点保存', true);
    });
  });
  $('#skip_duplicate_media').on('change', toggleDuplicateMinSize);
  $('#btn_save_chats').on('click', function () {
    var ids = chatRows.filter(function (r) { return r.checked; }).map(function (r) { return r.key; });
    var months = {};
    var sizes = {};
    var backups = {};
    chatRows.forEach(function (r) {
      if (r.checked) {
        months[String(r.key)] = r.startMonth || '';
        sizes[String(r.key)] = r.maxSize || '';
        backups[String(r.key)] = { enabled: !!r.backup, end_month: r.backupEnd || '' };
      }
    });
    var payload = {
      chat_ids: ids,
      max_file_sizes: sizes,
      start_months: months,
      skip_duplicate_media: $('#skip_duplicate_media').prop('checked'),
      duplicate_min_size_mb: $('#duplicate_min_size_mb').val(),
      backups: backups
    };
    api('POST', 'api/chats', payload).then(function () {
      toast('已保存 ' + ids.length + ' 个频道', true);
      refreshStatus();
    });
  });

  // ---- start / stop

  $('#btn_main').on('click', function () {
    var button = $(this);
    if (!status || button.prop('disabled')) {
      return;
    }
    button.prop('disabled', true);
    var request = status.state === 'downloading'
      ? api('POST', 'api/download/stop')
      : api('POST', 'api/download/start').then(function () { switchTab('downloading'); });
    request.always(refreshStatus);
  });

  $('#btn_backup').on('click', function () {
    var button = $(this);
    if (!status || button.prop('disabled')) {
      return;
    }
    button.prop('disabled', true);
    api('POST', 'api/backup/start').then(function () { switchTab('uploading'); }).always(refreshStatus);
  });

  // index.html's inline handler has already switched the pause state with a
  // synchronous request by now; re-render right away so the Chinese label
  // replaces the English one it writes without waiting for the next poll.
  $('#download_state').on('click', function () {
    refreshStatus();
  });

  // ---- native helpers (only inside the app window)
  //
  // There is no window.pywebview.api bridge any more (removed as a security
  // fix: it let page JS reach arbitrary Python objects). The two native
  // actions are plain token-guarded HTTP routes instead; the server tells
  // us whether they are backed by a real window via data-native on <body>.

  if (document.body.getAttribute('data-native') === '1') {
    $('body').addClass('has-pywebview');
  }

  $('#btn_choose_folder').on('click', function () {
    api('POST', 'api/native/choose_folder').then(function (path) {
      if (path) {
        $('#settings_form [name=save_path]').val(path);
      }
    });
  });
  $('.gui-open-log').on('click', function () {
    api('POST', 'api/native/open_log_folder');
  });

  // ---- browser address

  $('#gui_url').val(window.location.origin + '/?token=' + encodeURIComponent(token));
  $('#btn_copy_url').on('click', function () {
    document.getElementById('gui_url').select();
    document.execCommand('copy');
    toast('已复制，可以粘贴到浏览器打开', true);
  });

  loadSettings();
  refreshStatus();
  setInterval(refreshStatus, 1000);
});
