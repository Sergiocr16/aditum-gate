import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, BehaviorSubject } from 'rxjs';

export interface AppConfig {
  device: {
    deviceId: string;
    deviceName: string;
    scannerType: string;
    scannerScript: string;
  };
  hardware: {
    hasScreen: boolean;
    hasTwoCameras: boolean;
    isScreen: boolean;
    deviceName: string;
  };
  door: {
    doorType: string;
    doorId: string;
    placeName: string;
  };
  display: {
    clientLogoUrl: string;
    showCameraFeed: boolean;
  };
  api: {
    baseUrl: string;
  };
  polling: {
    intervalSeconds: number;
    enabled: boolean;
  };
}

@Injectable({
  providedIn: 'root'
})
export class ConfigService {
  private configSubject = new BehaviorSubject<AppConfig | null>(null);
  public config$ = this.configSubject.asObservable();

  constructor(private http: HttpClient) {
    this.loadConfig();
  }

  loadConfig(): void {
    // Fetch config from Node.js server which serves the cached config
    this.http.get<AppConfig>('http://localhost:3000/api/config')
      .subscribe({
        next: (config) => {
          this.configSubject.next(config);
        },
        error: (error) => {
          console.error('Error loading config:', error);
          // Use default config on error
          this.configSubject.next(this.getDefaultConfig());
        }
      });
  }

  getConfig(): AppConfig | null {
    return this.configSubject.value;
  }

  private getDefaultConfig(): AppConfig {
    return {
      device: {
        deviceId: 'DEVICE-001',
        deviceName: 'Default Device',
        scannerType: 'qr',
        scannerScript: 'scannerQr.py'
      },
      hardware: {
        hasScreen: true,
        hasTwoCameras: true,
        isScreen: true,
        deviceName: 'Newtologic  4010E'
      },
      door: {
        doorType: 'entry',
        doorId: '0',
        placeName: 'Name'
      },
      display: {
        clientLogoUrl: 'https://res.cloudinary.com/aditum/image/upload/v1501920877/fzncrputkdgm8iasuc3t.jpg',
        showCameraFeed: true
      },
      api: {
        baseUrl: 'https://app.aditumcr.com/api'
      },
      polling: {
        intervalSeconds: 30,
        enabled: true
      }
    };
  }
}