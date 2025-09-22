import preprocess from 'svelte-preprocess';

export default {
  compilerOptions: {
    css: true,
    dev: process.env.NODE_ENV !== 'production'
  },
  preprocess: preprocess()
};
