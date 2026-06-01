#!/usr/bin/env node
/**
 * Add Xedex technical meeting to Google Calendar
 * Meeting: Wednesday, January 14, 2026 at 10:00 h
 */

import { google } from 'googleapis';
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TOKEN_PATH = path.join(process.env.HOME, '.config', 'google-calendar-mcp', 'tokens.json');
const CREDENTIALS_PATH = path.join(process.env.HOME, '.gmail-mcp', 'gcp-oauth.keys.json');

async function loadTokens() {
  try {
    const content = await fs.readFile(TOKEN_PATH, 'utf-8');
    const tokens = JSON.parse(content);
    // Handle multi-account format
    if (tokens.normal) {
      return tokens.normal;
    }
    return tokens;
  } catch (error) {
    throw new Error(`Error loading tokens: ${error.message}`);
  }
}

async function loadCredentials() {
  try {
    const content = await fs.readFile(CREDENTIALS_PATH, 'utf-8');
    const keys = JSON.parse(content);
    
    if (keys.installed) {
      return {
        clientId: keys.installed.client_id,
        clientSecret: keys.installed.client_secret,
        redirectUri: keys.installed.redirect_uris[0] || 'http://localhost:3500/oauth2callback'
      };
    } else if (keys.client_id && keys.client_secret) {
      return {
        clientId: keys.client_id,
        clientSecret: keys.client_secret,
        redirectUri: keys.redirect_uris?.[0] || 'http://localhost:3500/oauth2callback'
      };
    }
    throw new Error('Invalid credentials format');
  } catch (error) {
    throw new Error(`Error loading credentials: ${error.message}`);
  }
}

async function createEvent() {
  try {
    const credentials = await loadCredentials();
    const tokens = await loadTokens();

    const oauth2Client = new google.auth.OAuth2(
      credentials.clientId,
      credentials.clientSecret,
      credentials.redirectUri
    );

    oauth2Client.setCredentials(tokens);

    // Refresh token if needed
    if (tokens.expiry_date && tokens.expiry_date <= Date.now()) {
      const { credentials: newTokens } = await oauth2Client.refreshAccessToken();
      oauth2Client.setCredentials(newTokens);
    }

    const calendar = google.calendar({ version: 'v3', auth: oauth2Client });

    const event = {
      summary: 'Reunión técnica – Humedades y Libro del Edificio | Passatge d\'Alió 18',
      description: 'Technical meeting to review humidity issues, building defects in terrace waterproofing (planta primera), and Libro del Edificio documentation.\n\nParticipants:\n- Rubén Álvarez (Xedex, Jefe de obra)\n- Antonio Petschen (Xedex)\n- Ricard Fayos (COAC, Dirección Facultativa)\n- Xavi Noves (IKC Immobles)\n\nBuilding completion date: 02/12/2021',
      location: 'Passatge d\'Alió 18, Barcelona',
      start: {
        dateTime: '2026-01-14T10:00:00',
        timeZone: 'Europe/Madrid',
      },
      end: {
        dateTime: '2026-01-14T12:00:00',
        timeZone: 'Europe/Madrid',
      },
      attendees: [
        { email: 'ruben.alvarez@xedexsa.com', displayName: 'Rubén Álvarez' },
        { email: 'a.petschen@xedexsa.com', displayName: 'Antonio Petschen' },
        { email: 'ricardfayos@coac.net', displayName: 'Ricard Fayos' },
        { email: 'xnoves@ikcimmobles.com', displayName: 'Xavi Noves' },
        { email: 'ana.serrano.solsona@gmail.com', displayName: 'Ana Serrano Solsona' },
      ],
      reminders: {
        useDefault: false,
        overrides: [
          { method: 'email', minutes: 24 * 60 }, // 1 day before
          { method: 'popup', minutes: 15 }, // 15 minutes before
        ],
      },
    };

    const response = await calendar.events.insert({
      calendarId: 'primary',
      requestBody: event,
      sendUpdates: 'all', // Send invitations to all attendees
    });

    console.log('Event created successfully!');
    console.log('Event ID:', response.data.id);
    console.log('Event URL:', response.data.htmlLink);
    console.log('Start:', response.data.start?.dateTime || response.data.start?.date);
    console.log('Summary:', response.data.summary);
    
    return response.data;
  } catch (error) {
    console.error('Error creating event:', error.message);
    if (error.response) {
      console.error('Error details:', JSON.stringify(error.response.data, null, 2));
    }
    process.exit(1);
  }
}

createEvent();

