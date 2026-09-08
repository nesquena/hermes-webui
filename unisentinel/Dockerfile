FROM node:18-alpine

# Create app directory
WORKDIR /usr/src/app

# Install dependencies
COPY package.json package-lock.json* ./
RUN npm install --production || true

# Bundle app source
COPY . .

EXPOSE 3000

CMD [ "npm", "start" ]
