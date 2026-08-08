(() => {
  'use strict';

  const state = {
    bootstrap: null,
    conversations: [],
    currentId: null,
    currentHasMessages: false,
    boundModelId: '',
    draftModelId: '',
    draftRequestId: createRequestId(),
    attachments: [],
    sending: false,
    remoteProcessing: false,
    hydrating: true,
    navigationBusy: false,
    deletingCurrent: false,
    processingPollTimer: null,
    processingPollGeneration: 0,
    cancelledConversationIds: new Set(),
    creatingConversation: null,
    authMode: 'login',
    activeConversationAction: null,
    confirmResolver: null,
    confirmReturnFocus: null,
    renameResolver: null,
    renameReturnFocus: null,
    activeModeIndex: 0,
    submenuModeId: '',
    submenuParent: null,
    menuCloseTimer: null,
    activePanel: 'workspace',
    draftSaveTimer: null,
    settingsSaveTimer: null,
    restoringSettings: false,
    restoringDraft: false,
    restoredCurrentId: '',
  };

  const $ = (id) => document.getElementById(id);
  const elements = {
    sidebar: $('sidebar'), brandLink: $('brandLink'),
    conversationList: $('conversationList'), conversationTitle: $('conversationTitle'), modelMode: $('modelMode'),
    messages: $('messages'), messageScroll: $('messageScroll'), welcome: $('welcome'), welcomeTitle: $('welcomeTitle'), welcomeText: $('welcomeText'),
    composer: $('composer'), prompt: $('prompt'), sendButton: $('sendButton'),
    fileInput: $('fileInput'), fileButton: $('fileButton'), attachmentTray: $('attachmentTray'), dropOverlay: $('dropOverlay'),
    modelPicker: $('modelPicker'), modelPickerTrigger: $('modelPickerTrigger'), selectedModeName: $('selectedModeName'),
    selectedModelName: $('selectedModelName'), modeMenu: $('modeMenu'), modelSubmenu: $('modelSubmenu'),
    guestNotice: $('guestNotice'), accountButton: $('accountButton'), accountName: $('accountName'), accountHint: $('accountHint'),
    accountAvatar: $('accountAvatar'), settingsPanel: $('settingsPanel'), workspacePanel: $('workspacePanel'),
    appearance: $('appearance'), language: $('language'), defaultModel: $('defaultModel'), compactSidebar: $('compactSidebar'),
    saveHistory: $('saveHistory'), saveSettings: $('saveSettings'), settingsStatus: $('settingsStatus'), clearData: $('clearData'),
    authDialog: $('authDialog'), authForm: $('authForm'), authTitle: $('authTitle'), authText: $('authText'), authSwitch: $('authSwitch'),
    authSubmit: $('authSubmit'), authError: $('authError'), nameLabel: $('nameLabel'), username: $('username'),
    email: $('email'), password: $('password'),
    confirmDialog: $('confirmDialog'), confirmTitle: $('confirmTitle'), confirmText: $('confirmText'),
    confirmClose: $('confirmClose'), confirmCancel: $('confirmCancel'), confirmAccept: $('confirmAccept'),
    renameDialog: $('renameDialog'), renameForm: $('renameForm'), renameInput: $('renameInput'), renameError: $('renameError'),
    renameClose: $('renameClose'), renameCancel: $('renameCancel'), renameConfirm: $('renameConfirm'),
    conversationDialog: $('conversationDialog'), conversationDialogTitle: $('conversationDialogTitle'),
    conversationRename: $('conversationRename'), conversationDelete: $('conversationDelete'), conversationDialogCancel: $('conversationDialogCancel'),
  };

  function createRequestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function isMutation(method) {
    return !['GET', 'HEAD', 'OPTIONS'].includes(String(method || 'GET').toUpperCase());
  }

  async function api(url, options = {}) {
    const method = String(options.method || 'GET').toUpperCase();
    const formData = options.body instanceof FormData;
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (!formData && options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (isMutation(method) && state.bootstrap?.csrf_token) headers.set('X-CSRF-Token', state.bootstrap.csrf_token);
    const response = await fetch(url, {...options, method, credentials: 'same-origin', headers});
    let payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (payload.csrf_token && state.bootstrap) state.bootstrap.csrf_token = payload.csrf_token;
    if (!response.ok) {
      const errorMessage = typeof payload.error === 'object' ? payload.error?.message : payload.error;
      const error = new Error(errorMessage || payload.message || `Request failed (${response.status})`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function userRoutes() {
    return state.bootstrap?.user?.routes || null;
  }

  function homePath() { return userRoutes()?.home || '/'; }
  function settingsPath() { return userRoutes()?.settings || '/'; }
  function chatPath(id) { return userRoutes() ? `${userRoutes().chat_base}/${encodeURIComponent(id)}` : '/'; }

  function updateUrl(path, {replace = false} = {}) {
    if (window.location.pathname === path) return;
    window.history[replace ? 'replaceState' : 'pushState']({}, '', path);
  }

  function routeFromLocation() {
    const routes = userRoutes();
    if (!routes) return {panel: 'workspace', conversationId: ''};
    const pathname = decodeURIComponent(window.location.pathname);
    if (pathname === routes.settings) return {panel: 'settings', conversationId: ''};
    const prefix = `${routes.chat_base}/`;
    if (pathname.startsWith(prefix)) return {panel: 'workspace', conversationId: pathname.slice(prefix.length).split('/')[0]};
    return {panel: 'workspace', conversationId: ''};
  }

  function draftPayload() {
    return {
      prompt: elements.prompt.value,
      panel: state.activePanel,
      current_id: state.currentId || '',
      draft_model_id: state.draftModelId || '',
      draft_request_id: state.draftRequestId || '',
      attachment_ids: state.attachments.map((item) => item.id),
    };
  }

  async function saveDraftState({keepalive = false} = {}) {
    if (!state.bootstrap?.user || state.restoringDraft) return;
    if (state.draftSaveTimer) window.clearTimeout(state.draftSaveTimer);
    state.draftSaveTimer = null;
    try {
      await api('/api/state', {method: 'PUT', body: JSON.stringify(draftPayload()), keepalive});
    } catch (_) {
      // A later edit or bootstrap will retry; the main interaction should stay usable.
    }
  }

  function scheduleDraftSave(delay = 260) {
    if (!state.bootstrap?.user || state.restoringDraft) return;
    if (state.draftSaveTimer) window.clearTimeout(state.draftSaveTimer);
    state.draftSaveTimer = window.setTimeout(() => saveDraftState(), delay);
  }

  function restoreDraft(draft) {
    if (!state.bootstrap?.user || !draft || typeof draft !== 'object') return;
    state.restoringDraft = true;
    state.draftModelId = modelRunnable(modelById(draft.draft_model_id)) ? modelById(draft.draft_model_id)?.id : state.draftModelId;
    state.draftRequestId = String(draft.draft_request_id || state.draftRequestId);
    state.restoredCurrentId = String(draft.current_id || '');
    state.attachments = Array.isArray(draft.attachments) ? draft.attachments.slice(0, 10) : [];
    elements.prompt.value = String(draft.prompt || '').slice(0, Number(elements.prompt.maxLength || 12000));
    renderAttachments();
    resizePrompt();
    updateModelDisplay();
    state.restoringDraft = false;
  }

  async function applyCurrentRoute({replaceInvalid = false} = {}) {
    const route = routeFromLocation();
    if (route.panel === 'settings') {
      showPanel('settings', {updateHistory: false});
      return;
    }
    if (route.conversationId) {
      try {
        await openConversation(route.conversationId, {updateHistory: false});
        return;
      } catch (_) {
        if (replaceInvalid) updateUrl(homePath(), {replace: true});
      }
    }
    if (state.currentId) resetWorkspaceDraft({updateHistory: false});
    else showPanel('workspace', {updateHistory: false});
  }

  function applyTheme(value) {
    const resolved = value === 'system'
      ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
      : value;
    document.documentElement.dataset.theme = resolved;
  }

  function models() { return state.bootstrap?.models || []; }
  function modes() { return state.bootstrap?.modes || []; }
  function modelById(id) { return models().find((item) => item.id === id); }
  function modelRunnable(model) { return Boolean(model && model.runnable === true); }
  function firstRunnableModel() { return models().find((item) => modelRunnable(item)); }
  function modeById(id) { return modes().find((item) => item.id === id); }
  function selectedModel() { return state.currentId ? state.boundModelId : state.draftModelId; }

  function modelDisplayName(model) {
    if (!model) return 'Model unavailable';
    return `${model.name}${model.version ? ` ${model.version}` : ''}`;
  }

  function updateModelDisplay() {
    const selectedId = selectedModel();
    const model = modelById(selectedId);
    const mode = model ? modeById(model.mode) : null;
    if (!models().length) {
      elements.selectedModeName.textContent = 'No models installed';
      elements.selectedModelName.textContent = 'Install a valid model package';
      elements.modelMode.textContent = 'UNAVAILABLE';
    } else if (model && !modelRunnable(model)) {
      elements.selectedModeName.textContent = mode?.name || model.mode.replaceAll('_', ' ');
      elements.selectedModelName.textContent = `${modelDisplayName(model)} · ${model.availability_message || 'Local files missing'}`;
      elements.modelMode.textContent = 'UNAVAILABLE';
    } else if (!model && selectedId) {
      elements.selectedModeName.textContent = 'Model unavailable';
      elements.selectedModelName.textContent = selectedId;
      elements.modelMode.textContent = 'UNAVAILABLE';
    } else if (model) {
      elements.selectedModeName.textContent = mode?.name || model.mode.replaceAll('_', ' ');
      elements.selectedModelName.textContent = modelDisplayName(model);
      elements.modelMode.textContent = (mode?.name || model.mode).toUpperCase();
    } else {
      elements.selectedModeName.textContent = state.bootstrap?.models_available ? 'Choose a model' : 'Local files missing';
      elements.selectedModelName.textContent = state.bootstrap?.models_available ? 'No selection' : 'Installed model packages are not runnable';
      elements.modelMode.textContent = state.bootstrap?.models_available ? 'LOCAL' : 'UNAVAILABLE';
    }
    updateComposerAvailability();
  }

  function conversationBusy() {
    return Boolean(
      state.hydrating
      || state.navigationBusy
      || state.deletingCurrent
      || state.sending
      || state.remoteProcessing
    );
  }

  function updateComposerAvailability() {
    const usable = modelRunnable(modelById(selectedModel()));
    const busy = conversationBusy();
    elements.prompt.disabled = !usable || busy;
    elements.fileButton.disabled = !usable || busy;
    elements.modelPickerTrigger.disabled = !models().length || busy;
    elements.sendButton.disabled = !usable || busy;
    if (busy) {
      elements.prompt.placeholder = 'Thinking… wait for the current response.';
    } else if (!models().length) {
      elements.prompt.placeholder = 'Install a valid CrowAI model package to begin.';
    } else if (!state.bootstrap?.models_available) {
      elements.prompt.placeholder = 'Install the required local model/runtime files to begin.';
    } else if (!usable) {
      elements.prompt.placeholder = 'Choose a runnable local model.';
    } else {
      elements.prompt.placeholder = 'Type a message…';
    }
  }

  function ensurePendingMessage() {
    if (document.getElementById('pendingMessage')) return;
    const pending = document.createElement('article');
    pending.className = 'message assistant pending-message';
    pending.id = 'pendingMessage';
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = 'C';
    const body = document.createElement('div');
    body.className = 'message-body';
    body.textContent = 'Thinking…';
    pending.append(avatar, body);
    elements.messages.append(pending);
    elements.messageScroll.scrollTop = elements.messageScroll.scrollHeight;
  }

  function clearPendingMessage() {
    document.getElementById('pendingMessage')?.remove();
  }

  function stopProcessingPoll({clearRemote = false} = {}) {
    state.processingPollGeneration += 1;
    if (state.processingPollTimer) window.clearTimeout(state.processingPollTimer);
    state.processingPollTimer = null;
    if (clearRemote) state.remoteProcessing = false;
  }

  function startProcessingPoll(id) {
    stopProcessingPoll();
    const generation = state.processingPollGeneration;
    const poll = async () => {
      if (generation !== state.processingPollGeneration || state.currentId !== id) return;
      try {
        const payload = await api(`/api/conversations/${id}`);
        if (generation !== state.processingPollGeneration || state.currentId !== id) return;
        const active = Boolean(payload.processing?.active);
        state.remoteProcessing = active;
        if (active) {
          ensurePendingMessage();
          updateComposerAvailability();
          state.processingPollTimer = window.setTimeout(poll, 1200);
          return;
        }

        clearPendingMessage();
        elements.messages.replaceChildren();
        for (const message of payload.messages || []) appendMessage(message.role, message.content, message.payload || {});
        state.currentHasMessages = Boolean(payload.messages?.length);
        elements.conversationTitle.textContent = payload.conversation.title;
        setWelcomeState();
        updateComposerAvailability();
        await loadConversations();
      } catch (error) {
        if (generation !== state.processingPollGeneration || state.currentId !== id) return;
        if (error.status === 404) {
          stopProcessingPoll({clearRemote: true});
          clearPendingMessage();
          updateComposerAvailability();
          return;
        }
        state.processingPollTimer = window.setTimeout(poll, 1800);
      }
    };
    state.processingPollTimer = window.setTimeout(poll, 900);
  }

  function setWelcomeState() {
    const noInstalled = models().length === 0;
    const noRunnable = !noInstalled && !state.bootstrap?.models_available;
    elements.welcomeTitle.textContent = noInstalled ? 'No models installed' : (noRunnable ? 'Local model files missing' : 'How can I help?');
    elements.welcomeText.textContent = noInstalled
      ? 'Install a validated package under models/<mode>/<version>/, then restart CrowAI.'
      : (noRunnable
        ? 'Model packages are installed, but their package-local GGUF/runtime prerequisites are unavailable.'
        : 'Select a local model, attach files when needed, and start with one clear request.');
    elements.welcome.hidden = elements.messages.children.length > 0;
  }

  function populateSettingsModels() {
    elements.defaultModel.replaceChildren();
    for (const item of models()) {
      const mode = modeById(item.mode);
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = `${mode?.name || item.mode.replaceAll('_', ' ')} · ${modelDisplayName(item)}${modelRunnable(item) ? '' : ' · Local files missing'}`;
      option.disabled = !modelRunnable(item);
      elements.defaultModel.append(option);
    }
    if (!models().length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'No models installed';
      elements.defaultModel.append(option);
    }
  }

  function clearMenuCloseTimer() {
    if (state.menuCloseTimer) window.clearTimeout(state.menuCloseTimer);
    state.menuCloseTimer = null;
  }

  function scheduleSubmenuClose() {
    clearMenuCloseTimer();
    state.menuCloseTimer = window.setTimeout(() => closeSubmenu(), 180);
  }

  function setModeRovingTabindex(index) {
    const items = [...elements.modeMenu.querySelectorAll('[role="menuitem"]')];
    if (!items.length) return;
    state.activeModeIndex = (index + items.length) % items.length;
    items.forEach((item, itemIndex) => { item.tabIndex = itemIndex === state.activeModeIndex ? 0 : -1; });
  }

  function focusMode(index) {
    setModeRovingTabindex(index);
    const items = [...elements.modeMenu.querySelectorAll('[role="menuitem"]')];
    items[state.activeModeIndex]?.focus();
  }

  function populateModelPicker() {
    closePicker({returnFocus: false});
    elements.modeMenu.replaceChildren();
    const selected = modelById(selectedModel());
    const modeList = modes();
    state.activeModeIndex = Math.max(0, modeList.findIndex((mode) => mode.id === selected?.mode));

    modeList.forEach((mode, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mode-menu-item';
      button.setAttribute('role', 'menuitem');
      button.dataset.modeId = mode.id;
      button.dataset.modeIndex = String(index);
      button.tabIndex = index === state.activeModeIndex ? 0 : -1;
      const multiple = mode.models.length > 1;
      const runnableModels = mode.models.filter((item) => modelRunnable(item));
      button.disabled = runnableModels.length === 0;
      if (multiple) {
        button.setAttribute('aria-haspopup', 'menu');
        button.setAttribute('aria-expanded', 'false');
        button.setAttribute('aria-controls', 'modelSubmenu');
      }
      if (mode.id === selected?.mode) button.classList.add('selected');

      const copy = document.createElement('span');
      copy.className = 'mode-menu-copy';
      const name = document.createElement('strong');
      name.textContent = mode.name;
      const detail = document.createElement('small');
      detail.textContent = multiple
        ? `${runnableModels.length}/${mode.models.length} runnable`
        : `${modelDisplayName(mode.models[0])}${modelRunnable(mode.models[0]) ? '' : ' · Local files missing'}`;
      copy.append(name, detail);
      button.append(copy);
      if (multiple) {
        const chevron = document.createElement('span');
        chevron.className = 'mode-menu-chevron';
        chevron.textContent = '›';
        chevron.setAttribute('aria-hidden', 'true');
        button.append(chevron);
      }

      button.addEventListener('pointerenter', () => {
        clearMenuCloseTimer();
        setModeRovingTabindex(index);
        if (multiple) openSubmenu(mode, button, {focusFirst: false});
        else closeSubmenu();
      });
      button.addEventListener('pointerleave', (event) => {
        if (!elements.modelSubmenu.contains(event.relatedTarget)) scheduleSubmenuClose();
      });
      button.addEventListener('focus', () => {
        setModeRovingTabindex(index);
        if (multiple) openSubmenu(mode, button, {focusFirst: false});
        else closeSubmenu();
      });
      button.addEventListener('click', async () => {
        if (multiple) {
          openSubmenu(mode, button, {focusFirst: true});
        } else if (modelRunnable(mode.models[0])) {
          await requestModelSelection(mode.models[0].id);
        }
      });
      button.addEventListener('keydown', (event) => handleModeKeydown(event, mode, button, index));
      elements.modeMenu.append(button);
    });
    updateModelDisplay();
  }

  function openPicker({focusMenu = false} = {}) {
    if (!models().length) return;
    clearMenuCloseTimer();
    elements.modeMenu.hidden = false;
    elements.modelPickerTrigger.setAttribute('aria-expanded', 'true');
    if (focusMenu) focusMode(state.activeModeIndex);
  }

  function closeSubmenu() {
    clearMenuCloseTimer();
    elements.modelSubmenu.hidden = true;
    elements.modelSubmenu.replaceChildren();
    if (state.submenuParent) state.submenuParent.setAttribute('aria-expanded', 'false');
    state.submenuModeId = '';
    state.submenuParent = null;
  }

  function closePicker({returnFocus = false} = {}) {
    clearMenuCloseTimer();
    closeSubmenu();
    elements.modeMenu.hidden = true;
    elements.modelPickerTrigger.setAttribute('aria-expanded', 'false');
    if (returnFocus && !elements.modelPickerTrigger.disabled) elements.modelPickerTrigger.focus();
  }

  function placeSubmenu(parentButton) {
    const parentRect = parentButton.getBoundingClientRect();
    const submenuRect = elements.modelSubmenu.getBoundingClientRect();
    const margin = 8;
    let left = parentRect.right + margin;
    if (left + submenuRect.width > window.innerWidth - margin) left = parentRect.left - submenuRect.width - margin;
    left = Math.max(margin, Math.min(left, window.innerWidth - submenuRect.width - margin));
    let top = parentRect.top;
    if (top + submenuRect.height > window.innerHeight - margin) top = window.innerHeight - submenuRect.height - margin;
    top = Math.max(margin, top);
    elements.modelSubmenu.style.left = `${Math.round(left)}px`;
    elements.modelSubmenu.style.top = `${Math.round(top)}px`;
  }

  function openSubmenu(mode, parentButton, {focusFirst = false} = {}) {
    clearMenuCloseTimer();
    if (state.submenuParent && state.submenuParent !== parentButton) state.submenuParent.setAttribute('aria-expanded', 'false');
    state.submenuModeId = mode.id;
    state.submenuParent = parentButton;
    parentButton.setAttribute('aria-expanded', 'true');
    elements.modelSubmenu.replaceChildren();

    mode.models.forEach((model, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'model-submenu-item';
      button.setAttribute('role', 'menuitem');
      button.tabIndex = index === 0 ? 0 : -1;
      if (model.id === selectedModel()) button.classList.add('selected');
      button.disabled = !modelRunnable(model);
      const name = document.createElement('strong');
      name.textContent = model.name;
      const version = document.createElement('small');
      version.textContent = modelRunnable(model) ? (model.version || model.local_id) : `${model.version || model.local_id} · Local files missing`;
      button.append(name, version);
      button.addEventListener('click', async () => requestModelSelection(model.id));
      button.addEventListener('keydown', (event) => handleSubmenuKeydown(event, mode, parentButton, index));
      elements.modelSubmenu.append(button);
    });
    elements.modelSubmenu.hidden = false;
    requestAnimationFrame(() => {
      placeSubmenu(parentButton);
      if (focusFirst) elements.modelSubmenu.querySelector('[role="menuitem"]')?.focus();
    });
  }

  function handleModeKeydown(event, mode, button, index) {
    if (event.key === 'ArrowDown') {
      event.preventDefault(); focusMode(index + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault(); focusMode(index - 1);
    } else if (event.key === 'ArrowRight' && mode.models.length > 1) {
      event.preventDefault(); openSubmenu(mode, button, {focusFirst: true});
    } else if ((event.key === 'Enter' || event.key === ' ') && mode.models.length > 1) {
      event.preventDefault(); openSubmenu(mode, button, {focusFirst: true});
    } else if ((event.key === 'Enter' || event.key === ' ') && mode.models.length === 1 && modelRunnable(mode.models[0])) {
      event.preventDefault(); requestModelSelection(mode.models[0].id);
    } else if (event.key === 'Escape') {
      event.preventDefault(); closePicker({returnFocus: true});
    } else if (event.key === 'Tab') {
      closePicker({returnFocus: false});
    }
  }

  function setSubmenuRoving(index) {
    const items = [...elements.modelSubmenu.querySelectorAll('[role="menuitem"]')];
    if (!items.length) return;
    const resolved = (index + items.length) % items.length;
    items.forEach((item, itemIndex) => { item.tabIndex = itemIndex === resolved ? 0 : -1; });
    items[resolved].focus();
  }

  function handleSubmenuKeydown(event, mode, parentButton, index) {
    if (event.key === 'ArrowDown') {
      event.preventDefault(); setSubmenuRoving(index + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault(); setSubmenuRoving(index - 1);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault(); closeSubmenu(); parentButton.focus();
    } else if (event.key === 'Escape') {
      event.preventDefault(); closePicker({returnFocus: true});
    } else if (event.key === 'Tab') {
      closePicker({returnFocus: false});
    }
  }

  async function requestModelSelection(modelId) {
    if (!modelRunnable(modelById(modelId))) return;
    closePicker({returnFocus: true});
    if (state.currentId) {
      const confirmed = await openConfirmDialog({
        title: 'Change model?',
        text: 'Start a new draft with this model? The current conversation will remain in history.',
        confirmText: 'Start draft',
      });
      if (!confirmed) return;
      resetWorkspaceDraft({modelId});
      scheduleDraftSave();
      return;
    }
    if (modelId === selectedModel()) return;
    state.draftModelId = modelId;
    updateModelDisplay();
    scheduleDraftSave();
  }

  function resetWorkspaceDraft({modelId = '', updateHistory = true, clearPrompt = true} = {}) {
    stopProcessingPoll({clearRemote: true});
    clearPendingMessage();
    state.currentId = null;
    state.currentHasMessages = false;
    state.boundModelId = '';
    state.attachments = [];
    state.draftRequestId = createRequestId();
    state.creatingConversation = null;
    state.draftModelId = (modelRunnable(modelById(modelId)) ? modelById(modelId)?.id : '') || (modelRunnable(modelById(state.draftModelId)) ? modelById(state.draftModelId)?.id : '') || state.bootstrap?.default_model || firstRunnableModel()?.id || '';
    elements.messages.replaceChildren();
    elements.conversationTitle.textContent = 'New conversation';
    renderAttachments();
    renderConversationList();
    setWelcomeState();
    updateModelDisplay();
    if (clearPrompt) elements.prompt.value = '';
    resizePrompt();
    showPanel('workspace', {updateHistory: false});
    if (updateHistory) updateUrl(homePath());
    scheduleDraftSave();
  }

  async function loadBootstrap() {
    const payload = await api('/api/bootstrap');
    const previousDraft = state.draftModelId;
    state.bootstrap = payload;
    elements.prompt.maxLength = Number(payload.message_limit || 12000);
    populateSettingsModels();
    if (!state.currentId) {
      state.draftModelId = (modelRunnable(modelById(previousDraft)) ? modelById(previousDraft)?.id : '') || payload.default_model || firstRunnableModel()?.id || '';
    }
    populateModelPicker();
    loadSettingsForm();
    renderAccount();
    elements.brandLink.href = homePath();
    elements.guestNotice.hidden = Boolean(payload.user) || payload.guest_remaining > 0;
    setWelcomeState();
    updateModelDisplay();
  }

  function renderAccount() {
    const user = state.bootstrap?.user;
    if (user) {
      elements.accountName.textContent = user.display_name || user.email;
      elements.accountHint.textContent = `@${user.username} · ${user.email}`;
      elements.accountAvatar.textContent = (user.display_name || user.email || 'U').slice(0, 1).toUpperCase();
    } else {
      elements.accountName.textContent = 'Guest';
      elements.accountHint.textContent = state.bootstrap?.guest_remaining ? 'One question available' : 'Sign in to continue';
      elements.accountAvatar.textContent = 'G';
    }
  }

  async function loadConversations() {
    const payload = await api('/api/conversations');
    state.conversations = payload.conversations || [];
    if (state.currentId && !state.conversations.some((item) => item.id === state.currentId)) {
      resetWorkspaceDraft();
    } else {
      renderConversationList();
    }
  }

  function renderConversationList() {
    elements.conversationList.replaceChildren();
    if (!state.conversations.length) {
      const empty = document.createElement('p');
      empty.className = 'conversation-empty';
      empty.textContent = 'No saved conversations';
      elements.conversationList.append(empty);
      return;
    }
    for (const item of state.conversations) {
      const row = document.createElement('div');
      row.className = 'conversation-row';
      if (item.id === state.currentId) row.classList.add('active');

      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'conversation-item';
      open.textContent = item.title;
      open.title = item.title;
      open.addEventListener('click', () => openConversation(item.id));

      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'conversation-more';
      more.textContent = '⋯';
      more.setAttribute('aria-label', `Actions for ${item.title}`);
      more.setAttribute('aria-expanded', 'false');

      more.addEventListener('click', (event) => {
        event.stopPropagation();
        openConversationActions(item, more);
      });
      row.append(open, more);
      elements.conversationList.append(row);
    }
  }

  function openConversationActions(item, returnFocus) {
    state.activeConversationAction = {item, returnFocus};
    elements.conversationDialogTitle.textContent = item.title;
    elements.conversationDialog.showModal();
    window.setTimeout(() => elements.conversationRename.focus(), 0);
  }

  function closeConversationActions() {
    if (elements.conversationDialog.open) elements.conversationDialog.close();
    const returnFocus = state.activeConversationAction?.returnFocus;
    state.activeConversationAction = null;
    returnFocus?.focus();
  }

  function closeConversationMenus() {
    closeConversationActions();
  }

  async function openConversation(id, {updateHistory = true} = {}) {
    closeConversationMenus();
    stopProcessingPoll({clearRemote: true});
    state.navigationBusy = true;
    updateComposerAvailability();
    try {
      const payload = await api(`/api/conversations/${id}`);
      const preserveRestoredAttachments = state.restoredCurrentId === id;
      state.currentId = id;
      state.boundModelId = payload.conversation.model_id;
      state.currentHasMessages = Boolean(payload.messages?.length);
      state.remoteProcessing = Boolean(payload.processing?.active);
      if (!preserveRestoredAttachments) state.attachments = [];
      state.restoredCurrentId = '';
      elements.messages.replaceChildren();
      elements.conversationTitle.textContent = payload.conversation.title;
      for (const message of payload.messages || []) appendMessage(message.role, message.content, message.payload || {});
      if (state.remoteProcessing) ensurePendingMessage();
      renderAttachments();
      renderConversationList();
      setWelcomeState();
      updateModelDisplay();
      showPanel('workspace', {updateHistory: false});
      if (updateHistory) updateUrl(chatPath(id));
      scheduleDraftSave();
      elements.messageScroll.scrollTop = elements.messageScroll.scrollHeight;
      if (state.remoteProcessing) startProcessingPoll(id);
    } finally {
      state.navigationBusy = false;
      updateComposerAvailability();
    }
  }

  async function renameConversation(item) {
    closeConversationMenus();
    const title = await openRenameDialog(item.title);
    if (typeof title !== 'string') return;
    await api(`/api/conversations/${item.id}`, {method: 'PATCH', body: JSON.stringify({title})});
    if (state.currentId === item.id) elements.conversationTitle.textContent = title;
    await loadConversations();
  }

  async function deleteConversation(item) {
    closeConversationMenus();
    const confirmed = await openConfirmDialog({
      title: 'Konuşmayı silmek mi?',
      text: 'Konuşma ve ekli dosyalar kalıcı olarak silinecektir.',
      confirmText: 'Sil',
      danger: true,
    });
    if (!confirmed) return;
    const wasActive = state.currentId === item.id;
    state.cancelledConversationIds.add(item.id);
    if (wasActive) {
      // Keep the current conversation hard-locked while DELETE is waiting for
      // Core to cancel the exact in-flight model turn. Clearing the polling flag
      // must never create a small window where drag/drop or submit can start a
      // second request against a conversation that is still being torn down.
      state.deletingCurrent = true;
      stopProcessingPoll({clearRemote: true});
      updateComposerAvailability();
    }
    try {
      await api(`/api/conversations/${item.id}`, {method: 'DELETE'});
    } catch (error) {
      state.cancelledConversationIds.delete(item.id);
      if (wasActive && state.currentId === item.id) {
        // A fail-closed cancellation timeout means the conversation still
        // exists. Re-hydrate its persistent processing lease before unlocking
        // the composer so a live backend can never be hidden from the UI.
        try { await openConversation(item.id, {updateHistory: false}); } catch (_) { /* surface original delete error */ }
      }
      throw error;
    } finally {
      if (wasActive) {
        state.deletingCurrent = false;
        updateComposerAvailability();
      }
    }
    await loadConversations();
    if (wasActive) {
      const latest = state.conversations[0];
      if (latest) await openConversation(latest.id);
      else resetWorkspaceDraft();
    }
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value), window.location.origin);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_) {
      return '';
    }
  }

  function formatProductPrice(product) {
    const value = Number(product.price ?? product.total_cost);
    if (!Number.isFinite(value)) return '';
    const currency = String(product.currency || 'TRY').toUpperCase();
    try {
      return new Intl.NumberFormat(undefined, {style: 'currency', currency, maximumFractionDigits: 2}).format(value);
    } catch (_) {
      return `${value.toLocaleString()} ${currency}`;
    }
  }

  function renderProducts(products) {
    const grid = document.createElement('div');
    grid.className = 'product-grid';

    for (const product of products.slice(0, 10)) {
      if (!product || typeof product !== 'object') continue;
      const href = safeHttpUrl(product.url || product.link);
      const card = document.createElement(href ? 'a' : 'section');
      card.className = 'product-card';
      if (href) {
        card.href = href;
        card.target = '_blank';
        card.rel = 'noopener noreferrer';
      }

      const imageUrl = safeHttpUrl(product.image_url || product.image);
      if (imageUrl) {
        const frame = document.createElement('div');
        frame.className = 'product-image-frame';
        const image = document.createElement('img');
        image.className = 'product-image';
        image.src = imageUrl;
        image.alt = String(product.product_name || product.name || 'Product');
        image.loading = 'lazy';
        image.referrerPolicy = 'no-referrer';
        frame.append(image);
        card.append(frame);
      }

      const info = document.createElement('div');
      info.className = 'product-info';
      const title = document.createElement('strong');
      title.className = 'product-title';
      title.textContent = String(product.product_name || product.name || 'Product');
      info.append(title);

      const price = formatProductPrice(product);
      if (price) {
        const priceNode = document.createElement('div');
        priceNode.className = 'product-price';
        priceNode.textContent = price;
        info.append(priceNode);
      }

      const facts = [];
      if (product.seller) facts.push(String(product.seller));
      else if (product.domain) facts.push(String(product.domain));
      const rating = Number(product.rating);
      if (Number.isFinite(rating)) {
        const reviews = Number(product.review_count);
        facts.push(`★ ${rating.toFixed(1)}${Number.isFinite(reviews) ? ` (${Math.round(reviews).toLocaleString()})` : ''}`);
      }
      if (product.availability) facts.push(String(product.availability));
      if (product.delivery) facts.push(String(product.delivery));
      if (facts.length) {
        const meta = document.createElement('div');
        meta.className = 'product-meta';
        meta.textContent = facts.join(' · ');
        info.append(meta);
      }

      card.append(info);
      grid.append(card);
    }
    return grid;
  }

  function appendMessage(role, content, payload = {}) {
    const article = document.createElement('article');
    article.className = `message ${role === 'user' ? 'user' : 'assistant'}`;
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = role === 'user' ? 'Y' : 'C';
    const body = document.createElement('div');
    body.className = 'message-body';
    const text = document.createElement('div');
    text.className = 'message-text';
    text.textContent = String(content || '');
    body.append(text);

    if (Array.isArray(payload.products) && payload.products.length) {
      const products = renderProducts(payload.products);
      if (products.children.length) body.append(products);
    }

    if (Array.isArray(payload.sources) && payload.sources.length) {
      const sources = document.createElement('div');
      sources.className = 'source-list';
      for (const source of payload.sources.slice(0, 16)) {
        const href = safeHttpUrl(source.url || source.link);
        if (!href) continue;
        const link = document.createElement('a');
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = source.title || source.name || href;
        sources.append(link);
      }
      if (sources.children.length) body.append(sources);
    }

    if (Array.isArray(payload.artifacts)) {
      for (const artifact of payload.artifacts.slice(0, 20)) body.append(renderArtifact(artifact));
    }
    if (Array.isArray(payload.warnings) && payload.warnings.length) {
      const meta = document.createElement('div');
      meta.className = 'message-meta';
      meta.textContent = payload.warnings.join(' · ');
      body.append(meta);
    }
    article.append(avatar, body);
    elements.messages.append(article);
    setWelcomeState();
  }

  function renderArtifact(artifact) {
    const card = document.createElement('section');
    card.className = 'artifact-card';
    const head = document.createElement('div');
    head.className = 'artifact-head';
    const title = document.createElement('strong');
    title.textContent = artifact.title || artifact.filename || artifact.path || artifact.name || 'Artifact';
    const actions = document.createElement('div');
    actions.className = 'artifact-actions';
    const content = String(artifact.content || artifact.text || artifact.code || '');
    const copy = document.createElement('button');
    copy.type = 'button'; copy.textContent = 'Copy';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(content); copy.textContent = 'Copied'; }
      catch (_) { copy.textContent = 'Copy failed'; }
      window.setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
    });
    const download = document.createElement('button');
    download.type = 'button'; download.textContent = 'Save';
    download.addEventListener('click', () => {
      const blob = new Blob([content], {type: 'text/plain;charset=utf-8'});
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = String(artifact.filename || artifact.name || 'artifact.txt').replace(/[\\/:*?"<>|]+/g, '-');
      anchor.click();
      URL.revokeObjectURL(url);
    });
    actions.append(copy, download);
    head.append(title, actions);
    const pre = document.createElement('pre');
    pre.textContent = content;
    card.append(head, pre);
    return card;
  }

  async function ensureConversationForSend() {
    if (state.currentId) return state.currentId;
    if (state.creatingConversation) return state.creatingConversation;
    state.creatingConversation = api('/api/conversations', {
      method: 'POST',
      body: JSON.stringify({model_id: selectedModel(), request_id: state.draftRequestId}),
    }).then((payload) => {
      state.currentId = payload.id;
      state.boundModelId = payload.model_id;
      state.currentHasMessages = false;
      updateUrl(chatPath(payload.id));
      scheduleDraftSave();
      return payload.id;
    }).finally(() => { state.creatingConversation = null; });
    return state.creatingConversation;
  }

  async function submitPrompt(event) {
    event.preventDefault();
    if (conversationBusy() || !modelRunnable(modelById(selectedModel()))) return;
    const question = elements.prompt.value.trim();
    if (question.length < 2) return;
    state.sending = true;
    updateComposerAvailability();
    let id = '';
    try {
      id = await ensureConversationForSend();
      appendMessage('user', question);
      state.currentHasMessages = true;
      elements.prompt.value = '';
      resizePrompt();
      scheduleDraftSave();
      ensurePendingMessage();

      const payload = await api(`/api/conversations/${id}/ask`, {
        method: 'POST',
        body: JSON.stringify({
          question,
          model_id: state.boundModelId,
          language: elements.language.value || 'auto',
          attachment_ids: state.attachments.map((item) => item.id),
          request_id: createRequestId(),
        }),
      });
      if (state.cancelledConversationIds.has(id)) return;
      clearPendingMessage();
      appendMessage('assistant', payload.answer, payload.result || {});
      state.attachments = [];
      renderAttachments();
      scheduleDraftSave();
      await loadBootstrap();
      await loadConversations();
      const current = state.conversations.find((item) => item.id === id);
      if (current) elements.conversationTitle.textContent = current.title;
      renderConversationList();
    } catch (error) {
      clearPendingMessage();
      if (id && state.cancelledConversationIds.has(id)) {
        // Deletion intentionally cancels the in-flight backend request. Do not
        // render that expected cancellation into whichever conversation is now open.
      } else if (error.status === 409 && error.payload?.conversation_processing && id) {
        state.sending = false;
        await openConversation(id, {updateHistory: false});
      } else {
        appendMessage('assistant', error.message, {warnings: [error.payload?.request_id ? `Request ID: ${error.payload.request_id}` : error.message]});
        if (/sign in/i.test(error.message)) openAuth();
      }
    } finally {
      if (id) state.cancelledConversationIds.delete(id);
      state.sending = false;
      updateComposerAvailability();
      if (!elements.prompt.disabled) elements.prompt.focus();
    }
  }

  function resizePrompt() {
    elements.prompt.style.height = 'auto';
    elements.prompt.style.height = `${Math.min(elements.prompt.scrollHeight, 220)}px`;
  }

  async function uploadFiles(files) {
    const selected = Array.from(files || []).slice(0, 10);
    if (!selected.length || conversationBusy() || !modelRunnable(modelById(selectedModel()))) return;
    const form = new FormData();
    form.append('model_id', selectedModel());
    form.append('request_id', createRequestId());
    selected.forEach((file) => form.append('files', file, file.name));
    elements.fileButton.disabled = true;
    try {
      const payload = await api('/api/uploads', {method: 'POST', body: form});
      state.attachments.push(...(payload.attachments || []));
      renderAttachments();
      scheduleDraftSave();
    } catch (error) {
      appendMessage('assistant', error.message, {warnings: [error.message]});
    } finally {
      elements.fileInput.value = '';
      updateComposerAvailability();
    }
  }

  function renderAttachments() {
    elements.attachmentTray.replaceChildren();
    for (const item of state.attachments) {
      const chip = document.createElement('div');
      chip.className = 'attachment-chip';
      const name = document.createElement('span');
      name.textContent = item.name;
      const remove = document.createElement('button');
      remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', `Remove ${item.name}`);
      remove.addEventListener('click', () => {
        state.attachments = state.attachments.filter((value) => value.id !== item.id);
        renderAttachments();
        scheduleDraftSave();
      });
      chip.append(name, remove);
      elements.attachmentTray.append(chip);
    }
    elements.attachmentTray.hidden = state.attachments.length === 0;
  }

  function showPanel(name, {updateHistory = true} = {}) {
    state.activePanel = name === 'settings' ? 'settings' : 'workspace';
    elements.workspacePanel.classList.toggle('active', state.activePanel === 'workspace');
    elements.settingsPanel.classList.toggle('active', state.activePanel === 'settings');
    document.querySelectorAll('.nav-item').forEach((button) => {
      const active = button.dataset.panel === state.activePanel;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    if (updateHistory) {
      const target = state.activePanel === 'settings' ? settingsPath() : (state.currentId ? chatPath(state.currentId) : homePath());
      updateUrl(target);
    }
    scheduleDraftSave();
  }

  function loadSettingsForm() {
    state.restoringSettings = true;
    const settings = state.bootstrap?.settings || {};
    elements.appearance.value = settings.appearance || 'system';
    elements.language.value = settings.language || 'auto';
    elements.defaultModel.value = settings.default_model || state.bootstrap?.default_model || '';
    elements.compactSidebar.checked = Boolean(settings.compact_sidebar);
    elements.saveHistory.checked = settings.save_history !== false;
    applyTheme(elements.appearance.value);
    state.restoringSettings = false;
  }

  function scheduleSettingsSave() {
    if (state.restoringSettings || !state.bootstrap?.user) return;
    if (state.settingsSaveTimer) window.clearTimeout(state.settingsSaveTimer);
    elements.settingsStatus.textContent = 'Saving…';
    state.settingsSaveTimer = window.setTimeout(() => saveSettings({silent: true}), 280);
  }

  function settingsPayload() {
    return {
      appearance: elements.appearance.value,
      language: elements.language.value,
      default_model: elements.defaultModel.value,
      compact_sidebar: elements.compactSidebar.checked,
      save_history: elements.saveHistory.checked,
    };
  }

  async function saveSettings({silent = false, keepalive = false} = {}) {
    if (!state.bootstrap?.user) { if (!silent) openAuth(); return; }
    try {
      const payload = await api('/api/settings', {method: 'PUT', body: JSON.stringify(settingsPayload()), keepalive});
      state.bootstrap.settings = payload.settings;
      state.bootstrap.default_model = payload.settings.default_model || state.bootstrap.default_model;
      loadSettingsForm();
      elements.settingsStatus.textContent = silent ? 'Saved automatically' : 'Saved';
      window.setTimeout(() => { elements.settingsStatus.textContent = ''; }, 1800);
    } catch (error) {
      elements.settingsStatus.textContent = error.message;
    }
  }

  function openAuth() {
    elements.authError.textContent = '';
    elements.authDialog.showModal();
  }

  function renderAuthMode() {
    const create = state.authMode === 'register';
    elements.authTitle.textContent = create ? 'Create account' : 'Sign in';
    elements.authText.textContent = create
      ? 'Keep local history and continue without the guest limit.'
      : 'Continue conversations and keep local history.';
    elements.authSubmit.textContent = create ? 'Create account' : 'Sign in';
    elements.authSwitch.textContent = create ? 'I already have an account' : 'Create an account';
    elements.nameLabel.hidden = !create;
    elements.username.required = create;
    elements.password.autocomplete = create ? 'new-password' : 'current-password';
  }

  async function submitAuth(event) {
    event.preventDefault();
    elements.authError.textContent = '';
    const body = {email: elements.email.value, password: elements.password.value, username: elements.username.value, display_name: elements.username.value};
    try {
      const payload = await api(`/api/auth/${state.authMode}`, {method: 'POST', body: JSON.stringify(body)});
      if (payload.user?.routes?.home) updateUrl(payload.user.routes.home, {replace: true});
      elements.authDialog.close();
      elements.authForm.reset();
      await loadBootstrap();
      resetWorkspaceDraft({modelId: state.bootstrap.default_model, updateHistory: false});
      restoreDraft(state.bootstrap.draft);
      await loadConversations();
      await applyCurrentRoute({replaceInvalid: true});
    } catch (error) {
      elements.authError.textContent = error.message;
    }
  }

  function restoreDialogFocus(element) {
    if (element instanceof HTMLElement && element.isConnected) element.focus();
  }

  function openConfirmDialog({title, text, confirmText, danger = false}) {
    if (state.confirmResolver) resolveConfirm(false);
    state.confirmReturnFocus = document.activeElement;
    elements.confirmTitle.textContent = title;
    elements.confirmText.textContent = text;
    elements.confirmAccept.textContent = confirmText;
    elements.confirmAccept.classList.toggle('danger-confirm', danger);
    elements.confirmDialog.showModal();
    window.setTimeout(() => elements.confirmCancel.focus(), 0);
    return new Promise((resolve) => { state.confirmResolver = resolve; });
  }

  function resolveConfirm(value) {
    if (!state.confirmResolver) return;
    const resolve = state.confirmResolver;
    const returnFocus = state.confirmReturnFocus;
    state.confirmResolver = null;
    state.confirmReturnFocus = null;
    if (elements.confirmDialog.open) elements.confirmDialog.close();
    resolve(Boolean(value));
    restoreDialogFocus(returnFocus);
  }

  function openRenameDialog(currentTitle) {
    if (state.renameResolver) resolveRename(null);
    state.renameReturnFocus = document.activeElement;
    elements.renameError.textContent = '';
    elements.renameInput.value = String(currentTitle || '');
    elements.renameDialog.showModal();
    window.setTimeout(() => {
      elements.renameInput.focus();
      elements.renameInput.select();
    }, 0);
    return new Promise((resolve) => { state.renameResolver = resolve; });
  }

  function resolveRename(value) {
    if (!state.renameResolver) return;
    const resolve = state.renameResolver;
    const returnFocus = state.renameReturnFocus;
    state.renameResolver = null;
    state.renameReturnFocus = null;
    if (elements.renameDialog.open) elements.renameDialog.close();
    resolve(value);
    restoreDialogFocus(returnFocus);
  }

  async function clearData() {
    const confirmed = await openConfirmDialog({
      title: 'Clear local data?',
      text: 'All conversations and uploaded files for this account will be permanently deleted.',
      confirmText: 'Clear data',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api('/api/me/data', {method: 'DELETE'});
      resetWorkspaceDraft();
      await loadConversations();
      scheduleDraftSave();
    } catch (error) {
      elements.settingsStatus.textContent = error.message;
    }
  }

  elements.composer.addEventListener('submit', submitPrompt);
  elements.conversationRename.addEventListener('click', async () => {
    const item = state.activeConversationAction?.item;
    closeConversationActions();
    if (item) await renameConversation(item);
  });
  elements.conversationDelete.addEventListener('click', async () => {
    const item = state.activeConversationAction?.item;
    closeConversationActions();
    if (item) await deleteConversation(item);
  });
  elements.conversationDialogCancel.addEventListener('click', closeConversationActions);
  elements.conversationDialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    closeConversationActions();
  });

  elements.prompt.addEventListener('input', () => { resizePrompt(); scheduleDraftSave(); });
  elements.prompt.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      elements.composer.requestSubmit();
    }
  });
  elements.modelPickerTrigger.addEventListener('click', () => {
    if (elements.modeMenu.hidden) openPicker({focusMenu: false});
    else closePicker({returnFocus: false});
  });
  elements.modelPickerTrigger.addEventListener('keydown', (event) => {
    if (['Enter', ' ', 'ArrowDown'].includes(event.key)) {
      event.preventDefault(); openPicker({focusMenu: true});
    } else if (event.key === 'Escape') {
      closePicker({returnFocus: true});
    }
  });
  elements.modelSubmenu.addEventListener('pointerenter', clearMenuCloseTimer);
  elements.modelSubmenu.addEventListener('pointerleave', (event) => {
    if (!state.submenuParent?.contains(event.relatedTarget)) scheduleSubmenuClose();
  });
  elements.fileButton.addEventListener('click', () => elements.fileInput.click());
  elements.fileInput.addEventListener('change', () => uploadFiles(elements.fileInput.files));
  elements.brandLink.addEventListener('click', (event) => {
    event.preventDefault();
    resetWorkspaceDraft();
  });
  elements.accountButton.addEventListener('click', async () => {
    if (!state.bootstrap?.user) { openAuth(); return; }
    const confirmed = await openConfirmDialog({
      title: 'Çıkış yapmak mı?',
      text: 'Yalnızca yerel CrowAI oturumunuz kapatılacak. Konuşmalarınız bu bilgisayarda saklanmaya devam edecek.',
      confirmText: 'Çıkış yap',
    });
    if (confirmed) await api('/api/auth/logout', {method: 'POST'}).then(() => { state.bootstrap.user = null; window.location.href = '/'; });
  });
  elements.authSwitch.addEventListener('click', () => {
    state.authMode = state.authMode === 'login' ? 'register' : 'login';
    renderAuthMode();
  });
  elements.authForm.addEventListener('submit', submitAuth);
  elements.saveSettings.addEventListener('click', saveSettings);
  elements.clearData.addEventListener('click', clearData);
  elements.appearance.addEventListener('change', () => { applyTheme(elements.appearance.value); scheduleSettingsSave(); });
  for (const control of [elements.language, elements.defaultModel, elements.compactSidebar, elements.saveHistory]) {
    control.addEventListener('change', scheduleSettingsSave);
  }
  document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.panel === 'workspace') resetWorkspaceDraft();
    else showPanel('settings');
  }));

  elements.confirmAccept.addEventListener('click', () => resolveConfirm(true));
  elements.confirmCancel.addEventListener('click', () => resolveConfirm(false));
  elements.confirmClose.addEventListener('click', () => resolveConfirm(false));
  elements.confirmDialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    resolveConfirm(false);
  });
  elements.confirmDialog.addEventListener('close', () => {
    if (!state.confirmResolver) return;
    const resolve = state.confirmResolver;
    const returnFocus = state.confirmReturnFocus;
    state.confirmResolver = null;
    state.confirmReturnFocus = null;
    resolve(false);
    restoreDialogFocus(returnFocus);
  });

  elements.renameForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = elements.renameInput.value.trim();
    if (!value || value.length > 120) {
      elements.renameError.textContent = 'Enter between 1 and 120 characters.';
      return;
    }
    resolveRename(value);
  });
  elements.renameCancel.addEventListener('click', () => resolveRename(null));
  elements.renameClose.addEventListener('click', () => resolveRename(null));
  elements.renameDialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    resolveRename(null);
  });
  elements.renameDialog.addEventListener('close', () => {
    if (!state.renameResolver) return;
    const resolve = state.renameResolver;
    const returnFocus = state.renameReturnFocus;
    state.renameResolver = null;
    state.renameReturnFocus = null;
    resolve(null);
    restoreDialogFocus(returnFocus);
  });

  document.addEventListener('click', (event) => {
    if (!event.target.closest('#modelPicker') && !elements.modelSubmenu.contains(event.target)) closePicker({returnFocus: false});
  });
  elements.modelPicker.addEventListener('focusout', () => {
    window.setTimeout(() => {
      if (!elements.modelPicker.contains(document.activeElement) && !elements.modelSubmenu.contains(document.activeElement)) {
        closePicker({returnFocus: false});
      }
    }, 0);
  });
  window.addEventListener('resize', () => {
    if (!elements.modelSubmenu.hidden && state.submenuParent) placeSubmenu(state.submenuParent);
  });

  let dragDepth = 0;
  function hasDraggedFiles(event) {
    return [...(event.dataTransfer?.types || [])].includes('Files');
  }
  function hideDropOverlay() { dragDepth = 0; elements.dropOverlay.hidden = true; }
  document.addEventListener('dragenter', (event) => {
    if (!conversationBusy() && modelRunnable(modelById(selectedModel())) && hasDraggedFiles(event)) {
      event.preventDefault(); dragDepth += 1; elements.dropOverlay.hidden = false;
    }
  });
  document.addEventListener('dragover', (event) => {
    if (!conversationBusy() && modelRunnable(modelById(selectedModel())) && hasDraggedFiles(event)) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    }
  });
  document.addEventListener('dragleave', (event) => {
    if (!hasDraggedFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) elements.dropOverlay.hidden = true;
  });
  document.addEventListener('drop', (event) => {
    if (!conversationBusy() && modelRunnable(modelById(selectedModel())) && hasDraggedFiles(event)) {
      event.preventDefault();
      const files = event.dataTransfer.files;
      hideDropOverlay();
      uploadFiles(files);
    }
  });
  window.addEventListener('dragend', hideDropOverlay);
  window.addEventListener('blur', hideDropOverlay);
  window.addEventListener('popstate', () => { applyCurrentRoute({replaceInvalid: true}); });
  window.addEventListener('pagehide', () => {
    saveDraftState({keepalive: true});
    if (state.bootstrap?.user && !state.restoringSettings) saveSettings({silent: true, keepalive: true});
  });

  (async () => {
    renderAuthMode();
    try {
      await loadBootstrap();
      resetWorkspaceDraft({modelId: state.bootstrap.default_model, updateHistory: false});
      restoreDraft(state.bootstrap.draft);
      await loadConversations();
      if (state.bootstrap.user && window.location.pathname === '/') updateUrl(homePath(), {replace: true});
      await applyCurrentRoute({replaceInvalid: true});
    } catch (error) {
      appendMessage('assistant', error.message, {warnings: [error.message]});
    } finally {
      // Never enable the composer during initial route hydration. On F5 the Core
      // processing lease must be read first so an in-flight turn restores its
      // Thinking… state before the user can submit another message.
      state.hydrating = false;
      updateComposerAvailability();
    }
  })();
})();
