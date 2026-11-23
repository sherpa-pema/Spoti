// Spoti Offline Database Engine (IndexedDB)
// -------------------------------------------------------------
// Audio files are stored locally in the phone's browser storage.
// iPhone 7 acts purely as transient download proxy with 0 lingering bytes.
// -------------------------------------------------------------
export const SpotiDB = {
      dbName: 'SpotiOfflineDB',
      version: 1,
      db: null,

      async open() {
        if (this.db) return this.db;
        return new Promise((resolve, reject) => {
          const req = indexedDB.open(this.dbName, this.version);
          req.onupgradeneeded = (e) => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains('tracks')) {
              const store = db.createObjectStore('tracks', { keyPath: 'id' });
              store.createIndex('savedAt', 'savedAt', { unique: false });
            }
          };
          req.onsuccess = (e) => {
            this.db = e.target.result;
            resolve(this.db);
          };
          req.onerror = (e) => reject(e.target.error);
        });
      },

      async saveTrack(track) {
        const db = await this.open();
        return new Promise((resolve, reject) => {
          const tx = db.transaction('tracks', 'readwrite');
          const store = tx.objectStore('tracks');
          const record = {
            id: track.id,
            original_id: track.original_id || track.id,
            title: track.title || 'Unknown Title',
            artist: track.artist || 'Unknown Artist',
            duration: track.duration || '--:--',
            thumbnail: track.thumbnail || '',
            album: track.album || '',
            blob: track.blob,
            sizeBytes: track.blob ? track.blob.size : 0,
            sizeFormatted: track.sizeFormatted || (track.blob ? (track.blob.size / (1024 * 1024)).toFixed(1) + ' MB' : ''),
            savedAt: Date.now()
          };
          const req = store.put(record);
          req.onsuccess = () => resolve(record);
          req.onerror = (e) => reject(e.target.error);
        });
      },

      async getTrack(id) {
        try {
          const db = await this.open();
          return new Promise((resolve) => {
            const tx = db.transaction('tracks', 'readonly');
            const store = tx.objectStore('tracks');
            const req = store.get(id);
            req.onsuccess = () => {
              if (req.result) return resolve(req.result);
              // Fallback: match by original_id if query id is an external/PocketBase identifier
              const cursorReq = store.openCursor();
              cursorReq.onsuccess = (e) => {
                const cursor = e.target.result;
                if (cursor) {
                  if (cursor.value && (cursor.value.original_id === id || cursor.value.id === id)) {
                    return resolve(cursor.value);
                  }
                  cursor.continue();
                } else {
                  resolve(null);
                }
              };
              cursorReq.onerror = () => resolve(null);
            };
            req.onerror = () => resolve(null);
          });
        } catch (e) {
          return null;
        }
      },

      async getAllTracks() {
        try {
          const db = await this.open();
          return new Promise((resolve) => {
            const tx = db.transaction('tracks', 'readonly');
            const store = tx.objectStore('tracks');
            const req = store.getAll();
            req.onsuccess = () => {
              const tracks = req.result || [];
              tracks.sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
              resolve(tracks);
            };
            req.onerror = () => resolve([]);
          });
        } catch (e) {
          return [];
        }
      },

      async deleteTrack(id) {
        const db = await this.open();
        return new Promise((resolve, reject) => {
          const tx = db.transaction('tracks', 'readwrite');
          const store = tx.objectStore('tracks');
          const req = store.delete(id);
          req.onsuccess = () => resolve(true);
          req.onerror = (e) => reject(e.target.error);
        });
      },

      async count() {
        try {
          const db = await this.open();
          return new Promise((resolve) => {
            const tx = db.transaction('tracks', 'readonly');
            const store = tx.objectStore('tracks');
            const req = store.count();
            req.onsuccess = () => resolve(req.result || 0);
            req.onerror = () => resolve(0);
          });
        } catch (e) {
          return 0;
        }
      },

      async getAllTrackIds() {
        try {
          const db = await this.open();
          return new Promise((resolve) => {
            const tx = db.transaction('tracks', 'readonly');
            const store = tx.objectStore('tracks');
            const req = store.getAllKeys();
            req.onsuccess = () => resolve(req.result || []);
            req.onerror = () => resolve([]);
          });
        } catch (e) {
          return [];
        }
      },

      async getTracksMetadata() {
        try {
          const db = await this.open();
          return new Promise((resolve) => {
            const tx = db.transaction('tracks', 'readonly');
            const store = tx.objectStore('tracks');
            const req = store.openCursor();
            const results = [];
            req.onsuccess = (e) => {
              const cursor = e.target.result;
              if (cursor) {
                const val = cursor.value;
                // Exclude binary audio blob from memory heap
                results.push({
                  id: val.id,
                  original_id: val.original_id || val.id,
                  title: val.title || 'Unknown Title',
                  artist: val.artist || 'Unknown Artist',
                  duration: val.duration || '--:--',
                  thumbnail: val.thumbnail || '',
                  album: val.album || '',
                  sizeBytes: val.sizeBytes || 0,
                  sizeFormatted: val.sizeFormatted || '',
                  savedAt: val.savedAt || 0,
                  is_offline: true,
                  isOnDevice: true
                });
                cursor.continue();
              } else {
                results.sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
                resolve(results);
              }
            };
            req.onerror = () => resolve([]);
          });
        } catch (e) {
          return [];
        }
      }
    };

if (typeof window !== "undefined") {
  window.SpotiDB = SpotiDB;
}
