import { QMainWindow, QWidget, FlexLayout, QLabel, QPushButton, QLineEdit, QTextEdit, QListWidget, QListWidgetItem, QHBoxLayout, QVBoxLayout, QWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView } from '@nodegui/nodegui';
import { AgentClient, MessageRow, ScheduleRow, SettingsPayload } from './api';
import { initializeBackend, shutdownBackend } from './backend';

interface UIState {
  messages: MessageRow[];
  schedule: ScheduleRow[];
  settings: SettingsPayload | null;
  status: string;
}

export class AppUI {
  private window: QMainWindow;
  private client: AgentClient;
  private state: UIState = {
    messages: [],
    schedule: [],
    settings: null,
    status: 'Disconnected',
  };

  private messageList: QListWidget;
  private scheduleTable: QTableWidget;
  private statusLabel: QLabel;

  private messageInput: QTextEdit;
  private phonesInput: QTextEdit;
  private scheduleTimeInput: QLineEdit;
  private tokenInput: QLineEdit;

  constructor(baseUrl: string) {
    this.client = new AgentClient(baseUrl);
    this.window = new QMainWindow();
    this.window.setWindowTitle('iMessage AI Desktop');
    this.window.resize(1100, 720);

    const central = new QWidget();
    const layout = new QVBoxLayout();
    central.setLayout(layout);

    const header = new QWidget();
    const headerLayout = new QHBoxLayout();
    header.setLayout(headerLayout);

    this.statusLabel = new QLabel();
    this.statusLabel.setText('Status: Disconnected');

    this.tokenInput = new QLineEdit();
    this.tokenInput.setPlaceholderText('Bearer token (IMSG_AI_TOKEN)');

    const connectBtn = new QPushButton();
    connectBtn.setText('Connect');
    connectBtn.addEventListener('clicked', () => this.handleConnect());

    headerLayout.addWidget(this.statusLabel);
    headerLayout.addWidget(this.tokenInput);
    headerLayout.addWidget(connectBtn);
    headerLayout.addStretch(1);

    layout.addWidget(header);

    // Message list
    this.messageList = new QListWidget();
    layout.addWidget(this.messageList, 5);

    // Composer
    const composer = new QWidget();
    const composerLayout = new QVBoxLayout();
    composer.setLayout(composerLayout);

    this.messageInput = new QTextEdit();
    this.messageInput.setPlaceholderText('Message');

    this.phonesInput = new QTextEdit();
    this.phonesInput.setPlaceholderText('Phone numbers (comma or newline separated)');

    const scheduleRow = new QWidget();
    const scheduleLayout = new QHBoxLayout();
    scheduleRow.setLayout(scheduleLayout);

    this.scheduleTimeInput = new QLineEdit();
    this.scheduleTimeInput.setPlaceholderText('HH:MM');

    const sendBtn = new QPushButton();
    sendBtn.setText('Send Bulk');
    sendBtn.addEventListener('clicked', () => this.handleSendBulk());

    const scheduleBtn = new QPushButton();
    scheduleBtn.setText('Schedule');
    scheduleBtn.addEventListener('clicked', () => this.handleSchedule());

    scheduleLayout.addWidget(this.scheduleTimeInput);
    scheduleLayout.addWidget(sendBtn);
    scheduleLayout.addWidget(scheduleBtn);

    composerLayout.addWidget(this.messageInput);
    composerLayout.addWidget(this.phonesInput);
    composerLayout.addWidget(scheduleRow);

    layout.addWidget(composer, 2);

    // Schedule table
    this.scheduleTable = new QTableWidget(0, 4);
    this.scheduleTable.setHorizontalHeaderLabels(['Time', 'Phone', 'Message', 'Actions']);
    this.scheduleTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);
    layout.addWidget(this.scheduleTable, 2);

    // Footer controls
    const footer = new QWidget();
    const footerLayout = new QHBoxLayout();
    footer.setLayout(footerLayout);

    const refreshBtn = new QPushButton();
    refreshBtn.setText('Refresh State');
    refreshBtn.addEventListener('clicked', () => this.refreshState());

    footerLayout.addWidget(refreshBtn);
    footerLayout.addStretch(1);

    layout.addWidget(footer);
    this.window.setCentralWidget(central);

    this.client.on('socket:new_message', (payload) => this.appendMessage({
      timestamp: new Date().toISOString(),
      phone: payload.phone,
      direction: payload.chat_type || 'Incoming',
      message: payload.message,
    }));
    this.client.on('socket:message_sent', (payload) => this.appendMessage({
      timestamp: new Date().toISOString(),
      phone: payload.phone,
      direction: 'Sent',
      message: payload.message,
    }));
    this.client.on('socket:ai_stream', (payload) => this.appendMessage({
      timestamp: new Date().toISOString(),
      phone: payload.phone || payload.chat_guid,
      direction: 'AI',
      message: payload.text || payload.delta,
    }));
    this.client.on('socket:status', (status) => this.updateStatus(`Socket: ${status}`));
    this.client.on('socket:error', (err) => this.updateStatus(`Socket error: ${err}`));

    initializeBackend().catch((err) => {
      this.updateStatus(`Failed to start backend: ${err}`);
    });

    this.window.show();
  }

  private updateStatus(text: string) {
    this.state.status = text;
    this.statusLabel.setText(`Status: ${text}`);
  }

  private async handleConnect() {
    const token = this.tokenInput.text();
    this.client.setToken(token || null);
    try {
      const state = await this.client.fetchState();
      this.state.settings = state.settings;
      this.state.messages = state.messages;
      this.state.schedule = state.schedule;
      this.refreshMessageList();
      this.refreshScheduleTable();
      this.updateStatus('Connected');
      this.client.connectSocket();
    } catch (err) {
      this.updateStatus(`Failed to connect: ${err}`);
    }
  }

  private parsePhones(): string[] {
    return this.phonesInput
      .toPlainText()
      .split(/[,\n]/)
      .map((p) => p.trim())
      .filter(Boolean);
  }

  private async handleSendBulk() {
    const message = this.messageInput.toPlainText().trim();
    const phones = this.parsePhones();
    if (!message || phones.length === 0) {
      this.updateStatus('Message and phones required');
      return;
    }
    try {
      await this.client.sendBulk(message, phones);
      this.updateStatus('Bulk send queued');
    } catch (err) {
      this.updateStatus(`Bulk send failed: ${err}`);
    }
  }

  private async handleSchedule() {
    const message = this.messageInput.toPlainText().trim();
    const phones = this.parsePhones();
    const timeStr = this.scheduleTimeInput.text();
    if (!message || phones.length === 0 || !timeStr) {
      this.updateStatus('Message, phones, and time required');
      return;
    }
    try {
      await this.client.scheduleMessages(timeStr, message, phones);
      this.updateStatus('Schedule created');
      await this.refreshState();
    } catch (err) {
      this.updateStatus(`Schedule failed: ${err}`);
    }
  }

  private appendMessage(row: MessageRow) {
    this.state.messages.push(row);
    const item = new QListWidgetItem();
    item.setText(`[${row.timestamp}] ${row.direction} — ${row.phone}: ${row.message}`);
    this.messageList.addItem(item);
    this.messageList.scrollToBottom();
  }

  private refreshMessageList() {
    this.messageList.clear();
    for (const row of this.state.messages.slice(-500)) {
      const item = new QListWidgetItem();
      item.setText(`[${row.timestamp}] ${row.direction} — ${row.phone}: ${row.message}`);
      this.messageList.addItem(item);
    }
    this.messageList.scrollToBottom();
  }

  private refreshScheduleTable() {
    this.scheduleTable.setRowCount(0);
    this.scheduleTable.setRowCount(this.state.schedule.length);
    this.state.schedule.forEach((entry, idx) => {
      this.scheduleTable.setItem(idx, 0, new QTableWidgetItem(entry.time));
      this.scheduleTable.setItem(idx, 1, new QTableWidgetItem(entry.phone));
      this.scheduleTable.setItem(idx, 2, new QTableWidgetItem(entry.message));
      const cancelBtn = new QPushButton();
      cancelBtn.setText('Cancel');
      cancelBtn.addEventListener('clicked', async () => {
        await this.client.cancelSchedule(entry.id);
        this.updateStatus('Schedule cancelled');
        await this.refreshState();
      });
      this.scheduleTable.setCellWidget(idx, 3, cancelBtn);
    });
  }

  async refreshState() {
    try {
      const state = await this.client.fetchState();
      this.state.settings = state.settings;
      this.state.messages = state.messages;
      this.state.schedule = state.schedule;
      this.refreshMessageList();
      this.refreshScheduleTable();
      this.updateStatus('State refreshed');
    } catch (err) {
      this.updateStatus(`Refresh failed: ${err}`);
    }
  }

  cleanup() {
    shutdownBackend();
    this.client.disconnectSocket();
  }

  getWindow() {
    return this.window;
  }
}
