import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject } from 'rxjs';

export interface ScreenConfig {
  hasScreen: boolean;
  doorType?: string;       // 'ENTRY' | 'EXIT'
  clientLogoUrl?: string;
}

export interface AppConfig {
  deviceId: string;
  placeName: string;
  screen: ScreenConfig;
}

const DEFAULT_CONFIG: AppConfig = {
  deviceId: '',
  placeName: '',
  screen: { hasScreen: true, doorType: 'ENTRY', clientLogoUrl: '' },
};

@Injectable({
  providedIn: 'root',
})
export class ConfigService {
  private configSubject = new BehaviorSubject<AppConfig>(DEFAULT_CONFIG);
  public config$ = this.configSubject.asObservable();

  constructor(private http: HttpClient) {
    this.loadConfig();
  }

  loadConfig(): void {
    // El server.js local (que sirve esta app) expone el subset seguro de la
    // configuración del dispositivo.
    this.http.get<AppConfig>('/api/config').subscribe({
      next: (config) => this.configSubject.next(config),
      error: (error) => {
        console.error('Error cargando la configuración:', error);
        this.configSubject.next(DEFAULT_CONFIG);
      },
    });
  }

  getConfig(): AppConfig {
    return this.configSubject.value;
  }
}
