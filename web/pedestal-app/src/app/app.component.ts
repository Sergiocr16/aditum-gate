import { Component, OnInit } from '@angular/core';
import { WebSocketService, GateEntryDTO } from './websocket.service';
import { ConfigService } from './config.service';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-root',
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css'],
  standalone: true,
  imports: [CommonModule]
})
export class AppComponent implements OnInit {
  state: number = 1; // Estado inicial por defecto
  name: string = '';
  doorType: string = 'ENTRY';
  clientLogoUrl: string = '';

  constructor(
    private webSocketService: WebSocketService,
    private configService: ConfigService,
  ) {}

  ngOnInit() {
    this.configService.config$.subscribe((config) => {
      this.doorType = config.screen.doorType ?? 'ENTRY';
      this.clientLogoUrl = config.screen.clientLogoUrl ?? '';
    });

    this.webSocketService.gateEntry$.subscribe((data: GateEntryDTO) => {
      this.state = data.state ?? 1;
      this.name = data.name ?? '';
    });
  }
}
