/* Email authentication goes directly to Firebase. Crayon receives only ID tokens. */
export async function emailClient(config){
  const [{initializeApp},{getAuth,inMemoryPersistence,setPersistence,createUserWithEmailAndPassword,signInWithEmailAndPassword,sendEmailVerification,sendPasswordResetEmail,signOut,reload,deleteUser}]=await Promise.all([
    import('https://www.gstatic.com/firebasejs/12.4.0/firebase-app.js'),
    import('https://www.gstatic.com/firebasejs/12.4.0/firebase-auth.js')]);
  const auth=getAuth(initializeApp(config));await setPersistence(auth,inMemoryPersistence);
  return {
    async signIn(email,password){return (await signInWithEmailAndPassword(auth,email,password)).user;},
    async create(email,password){return (await createUserWithEmailAndPassword(auth,email,password)).user;},
    async verify(){if(!auth.currentUser)throw new Error('Sign in first.');await sendEmailVerification(auth.currentUser);},
    async reset(email){await sendPasswordResetEmail(auth,email);},
    async token(){if(!auth.currentUser)throw new Error('Sign in first.');await reload(auth.currentUser);if(!auth.currentUser.emailVerified)throw new Error('Verify your email first.');return auth.currentUser.getIdToken(true);},
    async deleteProvider(){if(!auth.currentUser)throw new Error('Sign in first.');await deleteUser(auth.currentUser);},
    async clear(){await signOut(auth);}
  };
}
