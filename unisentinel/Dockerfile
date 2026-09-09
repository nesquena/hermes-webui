FROM node:18-alpine

# Create app directory
WORKDIR /usr/src/app

# Install dependencies
COPY package*.json ./
RUN npm ci --only=production || npm install --only=production

# Bundle app
COPY . .

EXPOSE 3000

ENV NODE_ENV=production
CMD ["node", "src/server.js"]
