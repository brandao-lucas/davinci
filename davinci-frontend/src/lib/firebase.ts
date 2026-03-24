import { initializeApp, getApps, type FirebaseApp } from 'firebase/app';
import { getAuth, GoogleAuthProvider, type Auth } from 'firebase/auth';

let _app: FirebaseApp | undefined;
let _auth: Auth | undefined;

export function getFirebaseAuth(): Auth {
  if (_auth) return _auth;
  if (!_app) {
    const config = {
      apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
      authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
      projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
      storageBucket: process.env.NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET,
      messagingSenderId: process.env.NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID,
      appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
    };
    _app = getApps().length === 0 ? initializeApp(config) : getApps()[0];
  }
  _auth = getAuth(_app);
  return _auth;
}

export const googleProvider = new GoogleAuthProvider();
