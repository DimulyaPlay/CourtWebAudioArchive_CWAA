(function () {
  const REPLACE_TAG_RE = /<replace><old>([\s\S]*?)<\/old><new>([\s\S]*?)<\/new><rule>(\d+)<\/rule><\/replace>/gi;
  const SCROLL_RESET_MS = 2000;
  const QUEUE_POLL_MS = 30000;
  const EMPTY_TRANSCRIPT_TEXT = 'Выберите аудиозапись для просмотра протокола';
  const NO_TRANSCRIPT_TEXT = 'У выбранной записи отсутствует протокол.';
  const QUEUED_TRANSCRIPT_TEXT = 'Протокол находится в очереди распознавания, откройте эту страницу через несколько минут';

  const ui = {
    audio: document.getElementById('audioPlayer'),
    audioSource: document.getElementById('audioSource'),
    transcriptBox: $('#transcriptBox'),
    recordList: $('#recordList'),
    searchForm: $('#searchForm'),
    localSearch: $('#localSearch'),
    foundCount: $('#foundCount'),
    matchCounter: $('#matchCounter'),
    selectedTitle: $('#selectedTitle'),
    recordInfo: $('#recordInfo'),
    playPause: $('#playPause'),
    seekBar: document.getElementById('seekBar'),
    currentTime: document.getElementById('currentTime'),
    duration: document.getElementById('duration'),
    queueCounter: $('#queueCounter'),
    copyToastContainer: $('#copyToastContainer'),
    loadingOverlay: document.getElementById('loadingOverlay'),
    downloadTextBtn: $('#downloadTextBtn'),
    retranscribeBtn: $('#retranscribeBtn'),
    reapplyRulesBtn: $('#reapplyRulesBtn'),
    replacePopover: $('#replacePopover'),
    selectionPopover: $('#selectionPopover'),
    modalOriginalText: $('#modalOriginalText'),
    modalReplacementText: $('#modalReplacementText')
  };

  const modals = {
    retranscribe: bootstrap.Modal.getOrCreateInstance(document.getElementById('retranscribeModal')),
    createRule: bootstrap.Modal.getOrCreateInstance(document.getElementById('createRuleModal')),
    onboarding: bootstrap.Modal.getOrCreateInstance(document.getElementById('onboardingModal'))
  };

  const state = {
    currentRecordId: null,
    currentRecordPath: null,
    phrases: [],
    phraseElements: [],
    matchIndices: [],
    currentMatchIndex: -1,
    activePhraseIndex: -1,
    isUserScrolling: false,
    scrollResetTimer: null,
    recordRequestToken: 0,
    pendingOperations: 0
  };

  const audioCtx = new AudioContext();
  const sourceNode = audioCtx.createMediaElementSource(ui.audio);
  const gainNode = audioCtx.createGain();
  sourceNode.connect(gainNode).connect(audioCtx.destination);

  function escapeRegExp(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function normalizeQuery(value) {
    return String(value || '').trim().toLowerCase();
  }

  function showOverlay() {
    state.pendingOperations += 1;
    ui.loadingOverlay.style.display = 'flex';
  }

  function hideOverlay() {
    state.pendingOperations = Math.max(0, state.pendingOperations - 1);
    if (state.pendingOperations === 0) {
      ui.loadingOverlay.style.display = 'none';
    }
  }

  function setEmptyTranscript(message) {
    ui.transcriptBox.empty().append(
      $('<p>').addClass('text-muted text-center mt-5').text(message)
    );
  }

  function setActionButtons(recordId) {
    const hasRecord = Boolean(recordId);
    ui.downloadTextBtn.toggle(hasRecord).data('id', recordId || null);
    ui.retranscribeBtn.toggle(hasRecord).data('id', recordId || null);
    ui.reapplyRulesBtn.toggle(hasRecord).data('id', recordId || null);
  }

  function showToast(message, title) {
    const toastId = `toast-${Date.now()}`;
    const toast = $('<div>')
      .attr({
        id: toastId,
        role: 'alert',
        'aria-live': 'assertive',
        'aria-atomic': 'true'
      })
      .addClass('toast align-items-center text-bg-dark border-0 mb-2');

    const wrapper = $('<div>').addClass('d-flex').appendTo(toast);
    const body = $('<div>').addClass('toast-body').appendTo(wrapper);
    if (title) {
      $('<div>').addClass('fw-bold mb-1').text(title).appendTo(body);
    }
    $('<div>').text(message).appendTo(body);

    $('<button>')
      .attr({
        type: 'button',
        'data-bs-dismiss': 'toast',
        'aria-label': 'Закрыть'
      })
      .addClass('btn-close btn-close-white me-2 m-auto')
      .appendTo(wrapper);

    ui.copyToastContainer.append(toast);
    const instance = new bootstrap.Toast(document.getElementById(toastId), { delay: 3000 });
    toast.on('hidden.bs.toast', function () {
      toast.remove();
    });
    instance.show();
  }

  function apiErrorMessage(xhr, fallback) {
    return xhr && xhr.responseJSON && xhr.responseJSON.error ? xhr.responseJSON.error : fallback;
  }

  function getJSON(url, data) {
    return $.ajax({
      url,
      method: 'GET',
      data,
      dataType: 'json'
    });
  }

  function postJSON(url, payload) {
    return $.ajax({
      url,
      method: 'POST',
      data: JSON.stringify(payload || {}),
      contentType: 'application/json',
      dataType: 'json'
    });
  }

  function resetMatches() {
    state.matchIndices = [];
    state.currentMatchIndex = -1;
    ui.matchCounter.text('');
    ui.transcriptBox.find('.phrase-item').removeClass('match-active');
  }

  function setPlayerButtonState(isPlaying) {
    ui.playPause.text(isPlaying ? '⏸' : '▶');
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) {
      return '0:00';
    }
    const min = Math.floor(seconds / 60);
    const sec = Math.floor(seconds % 60);
    return `${min}:${sec < 10 ? '0' : ''}${sec}`;
  }

  function updateAudioTimeUI() {
    ui.seekBar.value = ui.audio.currentTime || 0;
    ui.currentTime.textContent = formatTime(ui.audio.currentTime);
  }

  function placePopover(popover, rect) {
    const popoverHeight = popover.outerHeight();
    const popoverWidth = popover.outerWidth();
    const viewportTop = window.scrollY;
    const showBelow = (rect.top + viewportTop) - viewportTop < popoverHeight + 20;
    const top = showBelow
      ? rect.bottom + window.scrollY + 6
      : rect.top + window.scrollY - popoverHeight - 6;
    const rawLeft = rect.left + window.scrollX + rect.width / 2 - popoverWidth / 2;
    const maxLeft = Math.max(10, window.innerWidth - popoverWidth - 10);

    popover
      .removeClass('bs-popover-top bs-popover-bottom')
      .addClass(showBelow ? 'bs-popover-bottom' : 'bs-popover-top')
      .css({
        top,
        left: Math.min(Math.max(rawLeft, 10), maxLeft),
        display: 'block'
      });
  }

  function appendHighlightedText(parent, text, query) {
    let hasMatch = false;
    const normalized = normalizeQuery(query);

    if (!normalized) {
      parent.appendChild(document.createTextNode(text));
      return hasMatch;
    }

    const pattern = new RegExp(escapeRegExp(normalized), 'gi');
    let lastIndex = 0;
    let match;

    while ((match = pattern.exec(text)) !== null) {
      hasMatch = true;
      if (match.index > lastIndex) {
        parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
      }
      const mark = document.createElement('mark');
      mark.textContent = match[0];
      parent.appendChild(mark);
      lastIndex = match.index + match[0].length;
    }

    if (lastIndex < text.length) {
      parent.appendChild(document.createTextNode(text.slice(lastIndex)));
    }

    return hasMatch;
  }

  function renderTaggedText(targetElement, taggedText, query) {
    targetElement.textContent = '';
    REPLACE_TAG_RE.lastIndex = 0;

    let hasMatch = false;
    let lastIndex = 0;
    let match;

    while ((match = REPLACE_TAG_RE.exec(taggedText)) !== null) {
      const [fullMatch, oldText, newText, ruleNum] = match;
      if (match.index > lastIndex) {
        hasMatch = appendHighlightedText(targetElement, taggedText.slice(lastIndex, match.index), query) || hasMatch;
      }

      const replacement = document.createElement('span');
      replacement.className = 'highlight-replace';
      replacement.dataset.old = oldText;
      replacement.dataset.rule = ruleNum;
      hasMatch = appendHighlightedText(replacement, newText, query) || hasMatch;
      targetElement.appendChild(replacement);

      lastIndex = match.index + fullMatch.length;
    }

    if (lastIndex < taggedText.length) {
      hasMatch = appendHighlightedText(targetElement, taggedText.slice(lastIndex), query) || hasMatch;
    }

    return hasMatch;
  }

  function renderPhraseAt(index, query) {
    const phrase = state.phrases[index];
    const element = state.phraseElements[index];
    if (!phrase || !element) {
      return false;
    }
    return renderTaggedText(element, phrase.text, query);
  }

  function centerElementInTranscript(element) {
    const $element = $(element);
    ui.transcriptBox.stop(true).animate({
      scrollTop: $element.position().top + ui.transcriptBox.scrollTop() - ui.transcriptBox.height() / 2 + $element.outerHeight() / 2
    }, 200);
  }

  function activatePhrase(index) {
    if (state.activePhraseIndex === index) {
      return;
    }

    if (state.activePhraseIndex >= 0 && state.phraseElements[state.activePhraseIndex]) {
      state.phraseElements[state.activePhraseIndex].classList.remove('phrase-active');
    }

    state.activePhraseIndex = index;
    if (index < 0 || !state.phraseElements[index]) {
      return;
    }

    const element = state.phraseElements[index];
    element.classList.add('phrase-active');
    if (!state.isUserScrolling) {
      centerElementInTranscript(element);
    }
  }

  function findPhraseIndexByTime(currentTime) {
    let left = 0;
    let right = state.phrases.length - 1;

    while (left <= right) {
      const mid = Math.floor((left + right) / 2);
      const phrase = state.phrases[mid];

      if (currentTime < phrase.start) {
        right = mid - 1;
      } else if (currentTime > phrase.end) {
        left = mid + 1;
      } else {
        return mid;
      }
    }

    return -1;
  }

  function syncActivePhrase() {
    if (!state.phrases.length) {
      activatePhrase(-1);
      return;
    }

    const currentTime = ui.audio.currentTime;
    let candidateIndex = state.activePhraseIndex;

    if (candidateIndex >= 0) {
      const currentPhrase = state.phrases[candidateIndex];
      if (currentTime >= currentPhrase.start && currentTime <= currentPhrase.end) {
        return;
      }
      if (currentTime > currentPhrase.end) {
        while (
          candidateIndex + 1 < state.phrases.length &&
          currentTime > state.phrases[candidateIndex].end
        ) {
          candidateIndex += 1;
        }
        if (
          candidateIndex < state.phrases.length &&
          currentTime >= state.phrases[candidateIndex].start &&
          currentTime <= state.phrases[candidateIndex].end
        ) {
          activatePhrase(candidateIndex);
          return;
        }
      }
    }

    activatePhrase(findPhraseIndexByTime(currentTime));
  }

  function scrollToMatch(index) {
    if (index < 0 || index >= state.matchIndices.length) {
      return;
    }

    const phraseIndex = state.matchIndices[index];
    const element = state.phraseElements[phraseIndex];
    if (!element) {
      return;
    }

    ui.transcriptBox.find('.phrase-item').removeClass('match-active');
    element.classList.add('match-active');
    centerElementInTranscript(element);
    ui.matchCounter.text(`${index + 1} из ${state.matchIndices.length}`);
  }

  function applyLocalSearch(query) {
    const normalized = normalizeQuery(query);
    resetMatches();

    state.phrases.forEach(function (_, index) {
      const hasMatch = renderPhraseAt(index, normalized);
      if (hasMatch) {
        state.matchIndices.push(index);
      }
    });

    if (!normalized) {
      return;
    }

    if (state.matchIndices.length) {
      state.currentMatchIndex = 0;
      scrollToMatch(0);
    } else {
      ui.matchCounter.text('0 совпадений');
    }
  }

  function buildRecordItem(record) {
    const item = $('<li>')
      .addClass('list-group-item list-group-item-action d-flex justify-content-between align-items-start')
      .attr({
        'data-id': record.id,
        'data-path': record.file_path
      });

    const checkWrap = $('<div>').addClass('form-check').appendTo(item);
    $('<input>')
      .addClass('form-check-input record-checkbox')
      .attr({
        type: 'checkbox',
        value: record.id
      })
      .appendTo(checkWrap);

    const info = $('<div>').addClass('ms-2 me-auto').appendTo(item);
    $('<div>')
      .addClass('fw-bold')
      .text(`${record.case_number} ${record.user_folder}`)
      .appendTo(info);
    $('<div>')
      .append($('<small>').text(new Date(record.date).toLocaleString()))
      .appendTo(info);
    $('<div>')
      .append($('<small>').addClass('text-muted').text(record.comment || ''))
      .appendTo(info);

    if (record.recognized_text_path) {
      $('<div>').addClass('text-primary fw-bold').text('Т').appendTo(item);
    }

    return item;
  }

  function renderSearchResults(records) {
    ui.recordList.empty();
    ui.foundCount.text(records.length);

    if (!records.length) {
      ui.recordList.append(
        $('<p>').addClass('text-muted text-center mt-3 mb-0').text('Ничего не найдено')
      );
      return;
    }

    const list = $('<ul>').addClass('list-group');
    records.forEach(function (record) {
      list.append(buildRecordItem(record));
    });
    ui.recordList.append(list);
  }

  function updateTranscript(data) {
    state.phrases = Array.isArray(data.phrases) ? data.phrases : [];
    state.phraseElements = [];
    state.activePhraseIndex = -1;
    resetMatches();

    ui.selectedTitle.text(data.title || '');
    ui.recordInfo.text(data.title || '');
    ui.audio.pause();
    setPlayerButtonState(false);
    ui.audioSource.setAttribute('src', data.audio_url || '');
    ui.audio.load();
    ui.seekBar.value = 0;
    ui.seekBar.max = 0;
    ui.currentTime.textContent = '0:00';
    ui.duration.textContent = '0:00';
    ui.transcriptBox.empty().scrollTop(0);

    if (!state.phrases.length) {
      setEmptyTranscript(data.is_in_recognition_queue ? QUEUED_TRANSCRIPT_TEXT : NO_TRANSCRIPT_TEXT);
      return;
    }

    const fragment = document.createDocumentFragment();
    state.phrases.forEach(function (phrase, index) {
      const element = document.createElement('div');
      element.className = 'phrase-item';
      element.dataset.start = phrase.start;
      element.dataset.end = phrase.end;
      element.dataset.index = index;
      fragment.appendChild(element);
      state.phraseElements.push(element);
    });
    ui.transcriptBox[0].appendChild(fragment);
    applyLocalSearch(ui.localSearch.val());
  }

  function loadRecord(recordId) {
    state.currentRecordId = recordId;
    const requestToken = ++state.recordRequestToken;

    getJSON(`/api/record/${recordId}`)
      .done(function (data) {
        if (requestToken !== state.recordRequestToken) {
          return;
        }
        updateTranscript(data);
      })
      .fail(function (xhr) {
        if (requestToken !== state.recordRequestToken) {
          return;
        }
        state.phrases = [];
        state.phraseElements = [];
        ui.selectedTitle.text('');
        ui.recordInfo.text('');
        setEmptyTranscript(apiErrorMessage(xhr, 'Не удалось загрузить протокол'));
        showToast(apiErrorMessage(xhr, 'Не удалось загрузить протокол'), 'Ошибка');
      });
  }

  function resetViewerState() {
    state.currentRecordId = null;
    state.currentRecordPath = null;
    state.phrases = [];
    state.phraseElements = [];
    state.activePhraseIndex = -1;
    ui.selectedTitle.text('');
    ui.recordInfo.text('');
    ui.audio.pause();
    ui.audioSource.setAttribute('src', '');
    ui.audio.load();
    setPlayerButtonState(false);
    ui.seekBar.value = 0;
    ui.seekBar.max = 0;
    ui.currentTime.textContent = '0:00';
    ui.duration.textContent = '0:00';
    ui.localSearch.val('');
    resetMatches();
    setActionButtons(null);
    setEmptyTranscript(EMPTY_TRANSCRIPT_TEXT);
  }

  function getSelectedRecordIds() {
    const selected = ui.recordList.find('.record-checkbox:checked').map(function () {
      return $(this).val();
    }).get();

    if (selected.length) {
      return selected;
    }

    return state.currentRecordId ? [String(state.currentRecordId)] : [];
  }

  function getSelectedPaths() {
    const selected = ui.recordList.find('.record-checkbox:checked').map(function () {
      return $(this).closest('.list-group-item').data('path');
    }).get();

    if (selected.length) {
      return selected;
    }

    return state.currentRecordPath ? [state.currentRecordPath] : [];
  }

  function getFileNameFromDisposition(disposition) {
    if (!disposition) {
      return null;
    }

    const utfMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utfMatch && utfMatch[1]) {
      return decodeURIComponent(utfMatch[1]);
    }

    const plainMatch = disposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/i);
    if (plainMatch && plainMatch[1]) {
      return plainMatch[1].replace(/['"]/g, '');
    }

    return null;
  }

  function downloadBlob(blob, filename) {
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'records.zip';
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
  }

  async function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }

    const tempArea = document.createElement('textarea');
    tempArea.value = value;
    document.body.appendChild(tempArea);
    tempArea.select();
    document.execCommand('copy');
    tempArea.remove();
  }

  function showOnboardingOnce() {
    if (localStorage.getItem('wasOnboardingShown')) {
      return;
    }

    localStorage.setItem('wasOnboardingShown', 'true');
    window.setTimeout(function () {
      modals.onboarding.show();
      window.setTimeout(function () {
        modals.onboarding.hide();
      }, 60000);
    }, 300);
  }

  function updateQueueCounter() {
    getJSON('/api/get_vr_queue_len')
      .done(function (data) {
        ui.queueCounter.text(`Протоколов в очереди: ${data.records_to_vr}`);
      })
      .fail(function () {
        ui.queueCounter.text('Протоколов в очереди: ?');
      });
  }

  ui.searchForm.on('submit', function (event) {
    event.preventDefault();
    resetViewerState();
    showOverlay();

    getJSON('/api/search', ui.searchForm.serialize())
      .done(renderSearchResults)
      .fail(function (xhr) {
        renderSearchResults([]);
        showToast(apiErrorMessage(xhr, 'Не удалось выполнить поиск'), 'Ошибка');
      })
      .always(hideOverlay);
  });

  $('#clearFilters').on('click', function () {
    ui.searchForm[0].reset();
    ui.recordList.empty();
    ui.foundCount.text('0');
    resetViewerState();
  });

  ui.localSearch.on('input', function () {
    applyLocalSearch($(this).val());
  });

  ui.recordList.on('click', '.list-group-item', function (event) {
    if ($(event.target).is('input')) {
      return;
    }

    const item = $(this);
    const recordId = item.data('id');
    state.currentRecordPath = item.data('path');
    ui.recordList.find('.list-group-item').removeClass('record-active');
    item.addClass('record-active');
    setActionButtons(recordId);
    showOnboardingOnce();
    loadRecord(recordId);
  });

  ui.transcriptBox.on('scroll', function () {
    state.isUserScrolling = true;
    clearTimeout(state.scrollResetTimer);
    state.scrollResetTimer = window.setTimeout(function () {
      state.isUserScrolling = false;
    }, SCROLL_RESET_MS);
  });

  ui.transcriptBox.on('click', '.phrase-item', function () {
    if (window.getSelection().toString().trim()) {
      return;
    }

    const start = Number.parseFloat(this.dataset.start);
    if (!Number.isNaN(start)) {
      ui.audio.currentTime = start;
      audioCtx.resume();
      ui.audio.play();
      setPlayerButtonState(true);
      syncActivePhrase();
    }
  });

  ui.transcriptBox.on('mouseenter', '.highlight-replace', function () {
    const target = $(this);
    const body = ui.replacePopover.find('.original-text');
    body.empty();
    body.append(document.createTextNode('Исходная фраза: '));
    $('<strong>').text(target.data('old')).appendTo(body);
    body.append('<br>');
    body.append(document.createTextNode(`Правило: ${target.data('rule')}`));
    ui.replacePopover.data('target', target);
    placePopover(ui.replacePopover, this.getBoundingClientRect());
  });

  ui.transcriptBox.on('mouseleave', '.highlight-replace', function () {
    window.setTimeout(function () {
      if (!ui.replacePopover.is(':hover')) {
        ui.replacePopover.hide();
      }
    }, 120);
  });

  ui.replacePopover.on('mouseleave', function () {
    ui.replacePopover.hide();
  });

  ui.transcriptBox.on('mouseup', function () {
    const selection = window.getSelection();
    const selectedText = selection ? selection.toString().trim() : '';

    if (!selectedText || !selection.rangeCount) {
      ui.selectionPopover.hide();
      return;
    }

    ui.selectionPopover.data('selectedText', selectedText);
    placePopover(ui.selectionPopover, selection.getRangeAt(0).getBoundingClientRect());
  });

  $(document).on('click', function (event) {
    if (!$(event.target).closest('#selectionPopover').length && !$(event.target).closest('.phrase-item').length) {
      ui.selectionPopover.hide();
    }
  });

  $('#createRuleBtn').on('click', function () {
    ui.modalOriginalText.val(ui.selectionPopover.data('selectedText') || '');
    ui.modalReplacementText.val('');
    ui.selectionPopover.hide();
    modals.createRule.show();
  });

  $('#confirmAddRuleBtn').on('click', function () {
    const from = ui.modalOriginalText.val().trim();
    const to = ui.modalReplacementText.val().trim();

    if (!state.currentRecordId) {
      showToast('Невозможно определить запись', 'Ошибка');
      return;
    }

    if (!to) {
      showToast('Введите текст замены', 'Ошибка');
      return;
    }

    const scrollTop = ui.transcriptBox.scrollTop();
    showOverlay();
    postJSON('/api/add_replacement_rule', {
      from,
      to,
      record_id: state.currentRecordId
    })
      .done(function (data) {
        modals.createRule.hide();
        showToast(`Создано правило: "${from}" → "${to}" (№${data.rule_index})`);
        loadRecord(state.currentRecordId);
        window.setTimeout(function () {
          ui.transcriptBox.scrollTop(scrollTop);
        }, 200);
      })
      .fail(function (xhr) {
        showToast(apiErrorMessage(xhr, 'Ошибка при добавлении правила'), 'Ошибка');
      })
      .always(hideOverlay);
  });

  ui.reapplyRulesBtn.on('click', function () {
    if (!state.currentRecordId) {
      showToast('ID записи не определён', 'Ошибка');
      return;
    }

    showOverlay();
    postJSON('/api/reapply_rules', { record_id: state.currentRecordId })
      .done(function () {
        showToast('Правила повторно применены к тексту');
        loadRecord(state.currentRecordId);
      })
      .fail(function (xhr) {
        showToast(apiErrorMessage(xhr, 'Ошибка при повторном применении правил'), 'Ошибка');
      })
      .always(hideOverlay);
  });

  ui.replacePopover.on('click', '.btn-undo-replacement', function () {
    const target = ui.replacePopover.data('target');
    if (!target || !target.length || !state.currentRecordId) {
      return;
    }

    showOverlay();
    postJSON('/api/undo_replacement', {
      record_id: state.currentRecordId,
      original: target.data('old'),
      rule: target.data('rule')
    })
      .done(function () {
        ui.replacePopover.hide();
        showToast('Изменения в документ внесены');
        loadRecord(state.currentRecordId);
      })
      .fail(function (xhr) {
        showToast(apiErrorMessage(xhr, 'Не удалось отменить замену'), 'Ошибка');
      })
      .always(hideOverlay);
  });

  $('#downloadSelected').on('click', function () {
    const ids = getSelectedRecordIds();
    if (!ids.length) {
      showToast('Не выделена ни одна запись для скачивания', 'Ошибка');
      return;
    }

    showOverlay();
    fetch(`/api/download?${ids.map(function (id) { return `id=${encodeURIComponent(id)}`; }).join('&')}`)
      .then(function (response) {
        if (!response.ok) {
          throw new Error(`Ошибка сервера: ${response.status}`);
        }
        return response.blob().then(function (blob) {
          return {
            blob,
            filename: getFileNameFromDisposition(response.headers.get('Content-Disposition'))
          };
        });
      })
      .then(function (result) {
        downloadBlob(result.blob, result.filename || 'records.zip');
      })
      .catch(function (error) {
        showToast(`Ошибка при скачивании: ${error.message}`, 'Ошибка');
      })
      .finally(hideOverlay);
  });

  $('#copyPaths').on('click', function () {
    const paths = getSelectedPaths();
    if (!paths.length) {
      showToast('Ничего не выбрано', 'Ошибка');
      return;
    }

    copyText(paths.join('\n'))
      .then(function () {
        showToast('Путь скопирован в буфер обмена', 'Готово');
      })
      .catch(function () {
        showToast('Не удалось скопировать путь', 'Ошибка');
      });
  });

  ui.downloadTextBtn.on('click', function () {
    if (!state.currentRecordId) {
      return;
    }
    window.location.href = `/api/export_text/${state.currentRecordId}?_=${Date.now()}`;
  });

  ui.retranscribeBtn.on('click', function () {
    if (state.currentRecordId) {
      modals.retranscribe.show();
    }
  });

  $('#confirmRetranscribe').on('click', function () {
    if (!state.currentRecordId) {
      return;
    }

    showOverlay();
    $.ajax({
      url: `/api/reset_transcription/${state.currentRecordId}`,
      method: 'POST',
      dataType: 'json'
    })
      .done(function () {
        modals.retranscribe.hide();
        showToast('Протокол отправлен на повторное распознавание');
        resetViewerState();
        ui.recordList.find('.record-active').removeClass('record-active');
        updateQueueCounter();
      })
      .fail(function (xhr) {
        showToast(apiErrorMessage(xhr, 'Не удалось отправить на повторное распознавание'), 'Ошибка');
      })
      .always(hideOverlay);
  });

  $('#nextMatch').on('click', function () {
    if (!state.matchIndices.length) {
      return;
    }
    state.currentMatchIndex = (state.currentMatchIndex + 1) % state.matchIndices.length;
    scrollToMatch(state.currentMatchIndex);
  });

  $('#prevMatch').on('click', function () {
    if (!state.matchIndices.length) {
      return;
    }
    state.currentMatchIndex = (state.currentMatchIndex - 1 + state.matchIndices.length) % state.matchIndices.length;
    scrollToMatch(state.currentMatchIndex);
  });

  ui.playPause.on('click', function () {
    if (!ui.audio.currentSrc) {
      return;
    }

    if (ui.audio.paused) {
      audioCtx.resume();
      ui.audio.play();
      setPlayerButtonState(true);
    } else {
      ui.audio.pause();
      setPlayerButtonState(false);
    }
  });

  $('#playbackRate').on('change', function () {
    ui.audio.playbackRate = Number.parseFloat(this.value) || 1;
  });

  $('#volumeSlider').on('input', function () {
    gainNode.gain.value = Number.parseFloat(this.value) || 1;
  });

  ui.seekBar.addEventListener('input', function () {
    ui.audio.currentTime = Number.parseFloat(this.value) || 0;
    updateAudioTimeUI();
    syncActivePhrase();
  });

  ui.audio.addEventListener('loadedmetadata', function () {
    ui.seekBar.max = Number.isFinite(ui.audio.duration) ? ui.audio.duration : 0;
    ui.duration.textContent = formatTime(ui.audio.duration);
  });

  ui.audio.addEventListener('timeupdate', function () {
    updateAudioTimeUI();
    syncActivePhrase();
  });

  ui.audio.addEventListener('seeked', syncActivePhrase);
  ui.audio.addEventListener('play', function () { setPlayerButtonState(true); });
  ui.audio.addEventListener('pause', function () { setPlayerButtonState(false); });
  ui.audio.addEventListener('ended', function () { setPlayerButtonState(false); });

  document.addEventListener('keydown', function (event) {
    const activeTag = document.activeElement && document.activeElement.tagName
      ? document.activeElement.tagName.toLowerCase()
      : '';

    if (activeTag === 'input' || activeTag === 'textarea' || !ui.audio.currentSrc) {
      return;
    }

    if (event.code === 'Space' || event.key === ' ') {
      event.preventDefault();
      ui.playPause.trigger('click');
    }
  });

  updateQueueCounter();
  window.setInterval(updateQueueCounter, QUEUE_POLL_MS);
  resetViewerState();
})();
