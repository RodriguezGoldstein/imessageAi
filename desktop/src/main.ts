import { QApplication } from '@nodegui/nodegui';
import path from 'path';
import { AppUI } from './ui';

QApplication.setApplicationName('iMessage AI Desktop');
QApplication.setApplicationVersion('0.1.0');

const baseUrl = process.env.IMSG_AI_BASE_URL || 'http://127.0.0.1:5000';

const app = QApplication.instance();
const ui = new AppUI(baseUrl);

app.aboutToQuit.connect(() => {
  ui.cleanup();
});

(global as any).win = ui.getWindow();
