import { io, Socket } from 'socket.io-client';
import { EventEmitter } from 'events';

export interface MessageRow {
  timestamp: string;
  phone: string;
  direction: string;
  message: string;
}

export interface ScheduleRow {
  id: string;
  time: string;
  phone: string;
  message: string;
}

export interface SettingsPayload {
  ai_trigger_tag: string;
  allowed_users: string[];
  openai_model: string;
  system_prompt: string;
  context_window: number;
  enable_search: boolean;
  search_max_results: number;
  image_chunk_size: number;
  [key: string]: unknown;
}

export interface AgentState {
  settings: SettingsPayload;
  messages: MessageRow[];
  schedule: ScheduleRow[];
}

export class AgentClient extends EventEmitter {
  private baseUrl: string;
  private token: string | null = null;
  private socket: Socket | null = null;

  constructor(baseUrl: string) {
    super();
    this.baseUrl = baseUrl;
  }

  setToken(token: string | null) {
    this.token = token;
  }

  private async request<T>(path: string, init: any = {}): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(init.headers || {}),
    };
    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }
    const response = await fetch(url, { ...init, headers });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || response.statusText);
    }
    if (response.status === 204) {
      return undefined as unknown as T;
    }
    return (await response.json()) as T;
  }

  async fetchState(): Promise<AgentState> {
    const [settings, messages, schedule] = await Promise.all([
      this.request<SettingsPayload>('/api/settings'),
      this.request<{ messages: MessageRow[] }>('/api/messages'),
      this.request<{ scheduled: ScheduleRow[] }>('/api/schedule'),
    ]);
    return {
      settings,
      messages: messages.messages,
      schedule: schedule.scheduled,
    };
  }

  async sendBulk(message: string, phones: string[]): Promise<void> {
    if (!phones.length) return;
    await Promise.all(
      phones.map((phone) =>
        this.request('/api/send', {
          method: 'POST',
          body: JSON.stringify({ phone, message }),
        }),
      ),
    );
  }

  async scheduleMessages(time: string, message: string, phones: string[]): Promise<void> {
    await this.request('/api/schedule', {
      method: 'POST',
      body: JSON.stringify({ time, message, phones }),
    });
  }

  async cancelSchedule(id: string): Promise<void> {
    await this.request(`/api/schedule/${id}`, { method: 'DELETE' });
  }

  async updateSettings(partial: Partial<SettingsPayload>): Promise<SettingsPayload> {
    return await this.request<SettingsPayload>('/api/settings', {
      method: 'PATCH',
      body: JSON.stringify(partial),
    });
  }

  connectSocket() {
    if (this.socket) return;
    this.socket = io(this.baseUrl, {
      auth: this.token ? { token: this.token } : undefined,
    });
    this.socket.on('connect', () => this.emit('socket:status', 'connected'));
    this.socket.on('disconnect', () => this.emit('socket:status', 'disconnected'));
    this.socket.on('connect_error', (err) => this.emit('socket:error', err));
    this.socket.on('new_message', (payload) => this.emit('socket:new_message', payload));
    this.socket.on('message_sent', (payload) => this.emit('socket:message_sent', payload));
    this.socket.on('ai_stream', (payload) => this.emit('socket:ai_stream', payload));
  }

  disconnectSocket() {
    if (!this.socket) return;
    this.socket.disconnect();
    this.socket = null;
  }
}
