// Thin wrapper around amazon-cognito-identity-js -- deliberately NOT
// aws-amplify, which drags in a much larger runtime (analytics, storage,
// etc.) for what amounts to "sign in with SRP, hold a session, refresh
// it". See template.yaml's AdminUserPool/AdminUserPoolClient comments
// for the pool-side half of this (no client secret -- GenerateSecret:
// false -- since a browser SPA can't keep one confidential) and
// src/admin_api_authorizer/app.py's decode_cognito_jwt for the
// server-side half that verifies whatever ID token this file hands the
// API client.
//
// DISCLOSED BUT UNTESTED: this sandbox has no npm registry access (see
// admin-spa/README.md), so amazon-cognito-identity-js was never
// actually installed or exercised here -- this file is written against
// its documented public API from memory/training data, not against a
// real import. Treat the exact shape of CognitoUser/CognitoUserSession
// calls below with a bit more scrutiny than code that was actually
// compiled, and smoke-test sign-in for real before relying on it.
import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserPool,
  type CognitoUserSession,
} from "amazon-cognito-identity-js";

const userPool = new CognitoUserPool({
  UserPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID,
  ClientId: import.meta.env.VITE_COGNITO_CLIENT_ID,
});

export interface AuthUser {
  email: string;
  // Fail-closed to match the backend authorizer's own posture
  // (build_user_context in src/admin_api_authorizer/app.py): an
  // authenticated Cognito account with no recognized group maps to
  // role=null here too, NOT a default "editor" -- see resolveRole
  // below. A null role can sign in (Cognito accepted the password) but
  // every admin_api call will 401, and the UI should say so rather than
  // pretend the account works.
  role: "admin" | "editor" | null;
}

function resolveRole(session: CognitoUserSession): AuthUser["role"] {
  const groups = (session.getIdToken().decodePayload()["cognito:groups"] as string[] | undefined) ?? [];
  if (groups.includes("Admins")) return "admin";
  if (groups.includes("Editors")) return "editor";
  return null;
}

function userFromSession(session: CognitoUserSession): AuthUser {
  const payload = session.getIdToken().decodePayload();
  return {
    email: (payload.email as string) ?? (payload["cognito:username"] as string) ?? "unknown",
    role: resolveRole(session),
  };
}

export function signIn(email: string, password: string): Promise<AuthUser> {
  return new Promise((resolve, reject) => {
    const cognitoUser = new CognitoUser({ Username: email, Pool: userPool });
    const authDetails = new AuthenticationDetails({ Username: email, Password: password });
    cognitoUser.authenticateUser(authDetails, {
      onSuccess: (session) => resolve(userFromSession(session)),
      onFailure: (err) => reject(err),
      // Admin-created accounts (AllowAdminCreateUserOnly: true in
      // template.yaml) start with a temporary password and Cognito
      // forces a change on first sign-in. Phase 1 doesn't build a
      // "set new password" form yet (see README.md's "not here yet"
      // section) -- surface a clear error rather than silently hanging.
      newPasswordRequired: () =>
        reject(
          new Error(
            "This account needs a new password set before first sign-in. Ask an admin to reset it via the Cognito console, or use the AWS CLI's admin-set-user-password.",
          ),
        ),
    });
  });
}

export function signOut(): void {
  userPool.getCurrentUser()?.signOut();
}

// Called on app load AND before every API request (see
// api/client.ts's authHeaders). CognitoUser.getSession() transparently
// refreshes an expired-but-not-yet-30-days-old session using the stored
// refresh token -- no separate "refresh" call needed on our end.
export function getCurrentUser(): Promise<AuthUser | null> {
  const cognitoUser = userPool.getCurrentUser();
  if (!cognitoUser) return Promise.resolve(null);
  return new Promise((resolve) => {
    cognitoUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session || !session.isValid()) {
        resolve(null);
        return;
      }
      resolve(userFromSession(session));
    });
  });
}

// Used only by api/client.ts -- returns null (rather than throwing) when
// there's no valid session, so the caller can turn that into a clean
// "please sign in again" rather than a confusing fetch failure.
export function getValidIdToken(): Promise<string | null> {
  const cognitoUser = userPool.getCurrentUser();
  if (!cognitoUser) return Promise.resolve(null);
  return new Promise((resolve) => {
    cognitoUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session || !session.isValid()) {
        resolve(null);
        return;
      }
      resolve(session.getIdToken().getJwtToken());
    });
  });
}
